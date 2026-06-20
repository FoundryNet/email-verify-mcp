"""Email verification engine — blocking DNS / SMTP work, called via asyncio.to_thread.

Resolves an email to deliverability signals from free primitives:
  • mx_valid       — the domain has MX (or fallback A) records (dnspython)
  • disposable     — domain is a known throwaway/temp-mail provider
  • role_account   — local part is a role address (info@, support@, …)
  • free_provider  — domain is a consumer free-mail provider (gmail, …)
  • domain_age_days— WHOIS creation date age (best-effort; None if unknown)
  • smtp_check     — RCPT TO probe against the MX (True/False/None; most cloud
                     egress blocks port 25, so this is usually None)
  • deliverable    — true / false / "unknown" rollup

Everything fails open: any probe that can't complete contributes None rather than
raising. Domain-level facts are cacheable (see core.py); email-level facts
(role_account, smtp_check, deliverable) are computed per call.
"""
from __future__ import annotations

import logging
import re
import smtplib
import socket
from datetime import datetime, timezone

import config

logger = logging.getLogger("ev.engine")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

ROLE_LOCALPARTS = {
    "info", "admin", "administrator", "support", "sales", "contact", "hello",
    "help", "billing", "accounts", "accounting", "postmaster", "webmaster",
    "abuse", "noreply", "no-reply", "donotreply", "do-not-reply", "team",
    "office", "mail", "marketing", "press", "media", "hr", "jobs", "careers",
    "security", "privacy", "legal", "compliance", "service", "services",
    "enquiries", "inquiries", "feedback", "newsletter", "notifications",
}

FREE_PROVIDERS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "ymail.com",
    "hotmail.com", "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com",
    "me.com", "mac.com", "proton.me", "protonmail.com", "pm.me", "gmx.com",
    "gmx.net", "mail.com", "yandex.com", "yandex.ru", "zoho.com", "zohomail.com",
    "fastmail.com", "tutanota.com", "tuta.io", "hey.com", "qq.com", "163.com",
    "126.com", "naver.com", "hotmail.co.uk", "live.co.uk", "comcast.net",
}

# A pragmatic core set of disposable / temp-mail domains. Extended at runtime from
# a bundled list file if present (disposable_domains.txt, one domain per line).
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "guerrillamail.net", "10minutemail.com",
    "tempmail.com", "temp-mail.org", "throwawaymail.com", "yopmail.com",
    "trashmail.com", "getnada.com", "nada.email", "dispostable.com", "fakeinbox.com",
    "maildrop.cc", "maildrop.cc", "sharklasers.com", "grr.la", "guerrillamailblock.com",
    "spam4.me", "mytemp.email", "mohmal.com", "emailondeck.com", "tempmailo.com",
    "moakt.com", "tmpmail.org", "tmpmail.net", "33mail.com", "mailnesia.com",
    "mintemail.com", "spamgourmet.com", "anonbox.net", "burnermail.io", "temp-mail.io",
    "tempr.email", "discard.email", "wegwerfmail.de", "einrot.com", "fakemail.net",
    "inboxbear.com", "vomoto.com", "tempinbox.com", "luxusmail.org", "mailcatch.com",
    "mvrht.com", "spambox.us", "trbvm.com", "mail-temp.com", "1secmail.com",
    "1secmail.org", "1secmail.net", "kzccv.com", "qiott.com", "wuuvo.com",
}


def _load_disposable_extra() -> None:
    """Augment DISPOSABLE_DOMAINS from a bundled list file, if present."""
    import os
    path = os.path.join(os.path.dirname(__file__), "disposable_domains.txt")
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                d = line.strip().lower()
                if d and not d.startswith("#"):
                    DISPOSABLE_DOMAINS.add(d)
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.info(f"disposable list load skipped: {e}")


_load_disposable_extra()


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def is_valid_syntax(email: str) -> bool:
    return bool(_EMAIL_RE.match(email or ""))


def split_email(email: str) -> tuple[str, str]:
    local, _, domain = (email or "").partition("@")
    return local, domain


# ── DNS / domain-level probes (cacheable per domain) ──────────────────────────
def mx_lookup(domain: str) -> tuple[bool, list]:
    """Return (mx_valid, mx_hosts). Falls back to A/AAAA (implicit MX per RFC 5321)."""
    try:
        import dns.resolver  # type: ignore
    except Exception as e:  # noqa: BLE001
        logger.info(f"dnspython unavailable: {e}")
        return (False, [])
    resolver = dns.resolver.Resolver()
    resolver.lifetime = 6.0
    resolver.timeout = 6.0
    try:
        answers = resolver.resolve(domain, "MX")
        hosts = sorted(
            ((int(r.preference), str(r.exchange).rstrip(".")) for r in answers),
            key=lambda x: x[0])
        mx = [h for _, h in hosts if h]
        if mx:
            return (True, mx[:5])
    except Exception:  # noqa: BLE001
        pass
    # Implicit MX: a domain with an A/AAAA record can still receive mail.
    for rtype in ("A", "AAAA"):
        try:
            resolver.resolve(domain, rtype)
            return (True, [domain])
        except Exception:  # noqa: BLE001
            continue
    return (False, [])


def whois_domain_age_days(domain: str) -> int | None:
    """Best-effort WHOIS creation-date age in days. None if unavailable."""
    try:
        import whois  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    try:
        w = whois.whois(domain)
        created = w.creation_date
        if isinstance(created, list):
            created = created[0] if created else None
        if not created:
            return None
        if isinstance(created, str):
            created = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - created).days)
    except Exception:  # noqa: BLE001
        return None


def smtp_probe(email: str, mx_hosts: list) -> bool | None:
    """Best-effort SMTP RCPT TO probe. Returns True (accepted), False (rejected),
    or None (could not verify — connection blocked/timed out/greylisted). Cloud
    egress commonly blocks port 25, so None is the typical result there."""
    if not config.SMTP_CHECK_ENABLED or not mx_hosts:
        return None
    for host in mx_hosts[:2]:
        try:
            server = smtplib.SMTP(timeout=config.SMTP_CHECK_TIMEOUT)
            server.connect(host, 25)
            server.helo("foundrynet.io")
            server.mail("verify@foundrynet.io")
            code, _ = server.rcpt(email)
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass
            if code in (250, 251):
                return True
            if code in (550, 551, 553, 554):
                return False
            return None  # greylist / ambiguous
        except (socket.timeout, OSError, smtplib.SMTPException):
            continue
        except Exception:  # noqa: BLE001
            continue
    return None


def domain_facts(domain: str) -> dict:
    """All cacheable, domain-level facts in one shot."""
    domain = (domain or "").strip().lower()
    mx_valid, mx_hosts = mx_lookup(domain)
    return {
        "domain": domain,
        "mx_valid": mx_valid,
        "mx_hosts": mx_hosts,
        "disposable": domain in DISPOSABLE_DOMAINS,
        "free_provider": domain in FREE_PROVIDERS,
        "domain_age_days": whois_domain_age_days(domain),
        "last_checked": datetime.now(timezone.utc).isoformat(),
    }


def verify(email: str, domain_row: dict) -> dict:
    """Combine cached domain facts with per-call email-level checks into a verdict."""
    email = normalize_email(email)
    local, domain = split_email(email)
    role_account = local in ROLE_LOCALPARTS or local.split("+")[0] in ROLE_LOCALPARTS
    mx_valid = bool(domain_row.get("mx_valid"))
    disposable = bool(domain_row.get("disposable"))

    smtp_check = smtp_probe(email, domain_row.get("mx_hosts") or []) if mx_valid else None

    if not mx_valid:
        deliverable: object = False
    elif smtp_check is True:
        deliverable = True
    elif smtp_check is False:
        deliverable = False
    else:
        deliverable = "unknown"

    return {
        "email": email,
        "deliverable": deliverable,
        "mx_valid": mx_valid,
        "disposable": disposable,
        "role_account": role_account,
        "free_provider": bool(domain_row.get("free_provider")),
        "domain_age_days": domain_row.get("domain_age_days"),
        "smtp_check": smtp_check,
    }
