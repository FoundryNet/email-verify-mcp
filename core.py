"""Shared logic behind the MCP tools and REST routes: the per-domain cache plus the
operations. Cache rule: a domain whose facts are missing or stale (>CACHE_TTL_DAYS)
triggers a live probe (DNS MX + WHOIS); otherwise the cached row is reused. Blocking
DNS/WHOIS/SMTP work runs in a worker thread.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import config
import daily_curator
import mint_integration
import payment_gate
import stripe_gate
import supa
import verify_engine as ve

logger = logging.getLogger("ev.core")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _fresh(row: Optional[dict]) -> bool:
    if not row:
        return False
    lc = _parse(row.get("last_checked"))
    return bool(lc and (_now() - lc) <= timedelta(days=config.CACHE_TTL_DAYS))


def _billing(decision: dict) -> dict:
    g = decision.get("gate")
    if g == "free":
        cap, cnt = decision.get("cap"), decision.get("count")
        return {"tier": "free", "used_today": cnt, "daily_free": cap,
                "remaining_today": (cap - cnt) if (cap is not None and cnt is not None) else None}
    if g == "paid":
        return {"tier": "paid", "charged_usdc": decision.get("amount_usdc")}
    if g == "api_key":
        return {"tier": "api_key", "note": "billed to your Forge account"}
    return {"tier": "free", "note": "gating inert"}


async def _domain_facts(domain: str) -> dict:
    """Return cached domain facts (fresh) or probe live + cache them."""
    row = await supa.get_domain(domain)
    if _fresh(row):
        return row
    facts = await asyncio.to_thread(ve.domain_facts, domain)
    await supa.upsert_domain(facts)
    return facts


async def _verify_one(email: str) -> dict:
    email = ve.normalize_email(email)
    if not ve.is_valid_syntax(email):
        return {"email": email, "deliverable": False, "valid_syntax": False,
                "mx_valid": False, "disposable": False, "role_account": False,
                "free_provider": False, "domain_age_days": None, "smtp_check": None}
    _, domain = ve.split_email(email)
    facts = await _domain_facts(domain)
    result = ve.verify(email, facts)
    result["valid_syntax"] = True
    result["cache"] = "hit" if _fresh(facts) else "miss"
    # Best-effort log for the daily brief stats (fire-and-forget).
    asyncio.create_task(supa.log_verification(domain, result["disposable"], result["deliverable"]))
    return result


# ── verify_email (PAID) ───────────────────────────────────────────────────────
async def do_verify_email(email: str, *, agent_key: str, payment_tx=None, api_key=None) -> dict:
    if not email or not str(email).strip():
        return {"error": "bad_request", "detail": "email is required"}
    e = ve.normalize_email(email)
    decision = await payment_gate.precheck("verify_email", {"email": e}, config.PRICE_VERIFY_EMAIL,
                                           agent_key, payment_tx, api_key)
    if decision["gate"] == "blocked":
        return decision["body"]
    result = await _verify_one(e)
    result["billing"] = _billing(decision)
    result["provenance"] = await asyncio.to_thread(
        mint_integration.attest_data, result, "analysis", "verify_email result")
    return result


# ── batch_verify (PAID, per-email pricing) ────────────────────────────────────
async def do_batch_verify(emails: list, *, agent_key: str, payment_tx=None, api_key=None) -> dict:
    if not emails or not isinstance(emails, list):
        return {"error": "bad_request", "detail": "emails (non-empty array) is required"}
    norm = []
    seen = set()
    for x in emails:
        e = ve.normalize_email(x)
        if e and e not in seen:
            seen.add(e)
            norm.append(e)
    norm = norm[:100]
    if not norm:
        return {"error": "bad_request", "detail": "no valid emails"}
    price = max(config.PRICE_BATCH_MIN, round(config.PRICE_BATCH_PER_EMAIL * len(norm), 6))
    decision = await payment_gate.precheck("batch_verify", {"emails": norm}, price,
                                           agent_key, payment_tx, api_key)
    if decision["gate"] == "blocked":
        return decision["body"]
    results = await asyncio.gather(*[_verify_one(e) for e in norm], return_exceptions=True)
    out = []
    for e, r in zip(norm, results):
        out.append(r if not isinstance(r, Exception) else {"email": e, "error": str(r)})
    deliverable = sum(1 for r in out if r.get("deliverable") is True)
    disposable = sum(1 for r in out if r.get("disposable"))
    return {"results": out, "count": len(out),
            "summary": {"deliverable": deliverable, "disposable": disposable,
                        "undeliverable_or_unknown": len(out) - deliverable},
            "price_usdc": price, "billing": _billing(decision)}


# ── B2B lead quality scoring (PAID) ──────────────────────────────────────────
async def _brand_domain_age(domain: str) -> Optional[int]:
    """Best-effort cross-enrichment of domain age from the brand-intel sibling
    (its domain_age is FREE). Always fail-open → None on any error/timeout."""
    if not domain:
        return None
    try:
        import http_util
        headers = {}
        if config.FNET_API_KEY:
            headers["Authorization"] = f"Bearer {config.FNET_API_KEY}"
        out = await http_util.request_json(
            "POST", f"{config.BRAND_INTEL_URL}/v1/age",
            headers=headers or None, body={"domain": domain}, timeout=4)
        if isinstance(out, dict) and "error" not in out:
            age = out.get("age_days")
            if isinstance(age, int) and age >= 0:
                return age
    except Exception:  # noqa: BLE001
        pass
    return None


async def _score_lead(email: str, domain: Optional[str] = None) -> dict:
    er = await _verify_one(email)
    if not domain:
        domain = email.split("@")[1] if "@" in email else None

    score = 50
    signals: list = []

    if er.get("deliverable") is True or er.get("mx_valid"):
        score += 10; signals.append("email_deliverable")
    else:
        score -= 20; signals.append("email_undeliverable")
    if er.get("disposable"):
        score -= 30; signals.append("disposable_email_provider")
    if er.get("role_account"):
        score -= 10; signals.append("role_account_not_personal")
    if er.get("free_provider"):
        score -= 15; signals.append("free_email_provider")

    # Domain age: prefer the local WHOIS value; cross-enrich from brand-intel only
    # if it returns a larger/valid age (fail-open to the local value).
    age = er.get("domain_age_days")
    brand_age = await _brand_domain_age(domain) if domain else None
    if isinstance(brand_age, int) and (age is None or brand_age > age):
        age = brand_age
    if age and age > 1825:
        score += 15; signals.append(f"established_domain_{age}d")
    elif age and age > 365:
        score += 5; signals.append(f"domain_{age}d_old")
    elif age is not None and age < 90:
        score -= 15; signals.append("new_domain_under_90d")

    score = max(0, min(100, score))
    if score >= 80:
        grade, rec = "A", "High quality lead — prioritize outreach"
    elif score >= 60:
        grade, rec = "B", "Good lead — standard follow-up"
    elif score >= 40:
        grade, rec = "C", "Moderate — verify before investing time"
    elif score >= 20:
        grade, rec = "D", "Low quality — likely not worth pursuing"
    else:
        grade, rec = "F", "Invalid or high-risk lead — skip"

    return {"lead_score": score, "grade": grade, "email": er.get("email", email),
            "domain": domain, "domain_age_days": age, "signals": signals,
            "email_details": er, "recommendation": rec}


async def do_lead_quality_score(email: str, domain: Optional[str] = None, *,
                                agent_key: str, payment_tx=None, api_key=None) -> dict:
    """B2B lead quality score (0-100, A-F) from email deliverability + domain credibility."""
    if not email or not str(email).strip():
        return {"error": "bad_request", "detail": "email is required"}
    e = ve.normalize_email(email)
    decision = await payment_gate.precheck("lead_quality_score", {"email": e},
                                           config.PRICE_LEAD_QUALITY, agent_key, payment_tx, api_key)
    if decision["gate"] == "blocked":
        return decision["body"]
    out = await _score_lead(e, domain)
    out["billing"] = _billing(decision)
    out["provenance"] = await asyncio.to_thread(
        mint_integration.attest_data, out, "analysis", "lead quality score")
    return out


async def do_batch_lead_score(leads: list, *, agent_key: str, payment_tx=None, api_key=None) -> dict:
    """Score a batch of B2B leads, ranked best-first."""
    if not leads or not isinstance(leads, list):
        return {"error": "bad_request", "detail": "leads (non-empty array) is required"}
    norm = []
    seen = set()
    for x in leads:
        e = ve.normalize_email(x)
        if e and e not in seen:
            seen.add(e)
            norm.append(e)
    norm = norm[:100]
    if not norm:
        return {"error": "bad_request", "detail": "no valid leads"}
    price = max(config.PRICE_BATCH_LEAD_MIN, round(config.PRICE_LEAD_PER_EMAIL * len(norm), 6))
    decision = await payment_gate.precheck("batch_lead_score", {"leads": norm}, price,
                                           agent_key, payment_tx, api_key)
    if decision["gate"] == "blocked":
        return decision["body"]
    results = await asyncio.gather(*[_score_lead(e) for e in norm], return_exceptions=True)
    out = []
    for e, r in zip(norm, results):
        out.append(r if not isinstance(r, Exception) else {"email": e, "error": str(r)})
    out.sort(key=lambda r: r.get("lead_score", -1), reverse=True)
    grades: dict = {}
    for r in out:
        g = r.get("grade")
        if g:
            grades[g] = grades.get(g, 0) + 1
    summary = {"grade_distribution": grades,
               "a_or_b_leads": grades.get("A", 0) + grades.get("B", 0),
               "avg_score": round(sum(r.get("lead_score", 0) for r in out if "lead_score" in r)
                                  / max(1, sum(1 for r in out if "lead_score" in r)), 1)}
    res = {"results": out, "count": len(out), "summary": summary,
           "price_usdc": price, "billing": _billing(decision)}
    res["provenance"] = await asyncio.to_thread(
        mint_integration.attest_data, res, "analysis", "batch lead quality score")
    return res


# ── daily_brief (premium, curated) ────────────────────────────────────────────
async def do_daily_brief(date, *, agent_key, payment_tx=None, api_key=None,
                         stripe_token=None) -> dict:
    day = (date or datetime.now(timezone.utc).strftime("%Y-%m-%d")).strip()

    # Stripe rail (parallel to x402): a paid Checkout Session unlocks the brief.
    stripe_err = None
    if stripe_token and stripe_gate.is_active():
        sv = await stripe_gate.verify_session(stripe_token, config.PRICE_DAILY_BRIEF,
                                              tool="daily_brief", agent_key=agent_key)
        if sv["ok"]:
            brief = await daily_curator.get_brief(day)
            if not brief:
                return {"error": "not_available",
                        "detail": f"No brief for {day} (not yet generated, or expired at midnight UTC). "
                                  f"Briefs are curated daily at {config.BRIEF_HOUR_UTC:02d}:00 UTC.",
                        "billing": "stripe"}
            await daily_curator.bump_purchase(day)
            return {**brief, "billing": "stripe", "stripe_session": sv["session"]}
        stripe_err = sv.get("detail")  # surface on the 402 below

    decision = await payment_gate.precheck("daily_brief", {"date": day},
                                           config.PRICE_DAILY_BRIEF, agent_key, payment_tx, api_key)
    if decision["gate"] == "blocked":
        return stripe_gate.augment_402(decision["body"], config.PRICE_DAILY_BRIEF,
                                       stripe_error=stripe_err)
    brief = await daily_curator.get_brief(day)
    if not brief:
        return {"error": "not_available",
                "detail": f"No brief for {day} (not yet generated, or expired at midnight UTC). "
                          f"Briefs are curated daily at {config.BRIEF_HOUR_UTC:02d}:00 UTC.",
                "billing": _billing(decision)}
    await daily_curator.bump_purchase(day)
    return {**brief, "billing": _billing(decision)}


def mint_info() -> dict:
    """FoundryNet Data Network + MINT Protocol attestation details (free)."""
    return {
        "network": "FoundryNet Data Network", **mint_integration.network_feed_block(),
        "message": ("Attest your agent's email/contact verification with MINT Protocol "
                    "for verifiable on-chain proof of work."),
        "positioning": ("A free-tier alternative to enterprise email verification "
                        "(ZeroBounce, NeverBounce, Hunter) — deliverability checks for "
                        "agents without enterprise subscriptions."),
        "mint_protocol": {"mcp_endpoint": "https://mint-mcp-production.up.railway.app/mcp",
                          "info_url": "https://mint.foundrynet.io",
                          "tools": ["mint_register", "mint_attest", "mint_verify",
                                    "mint_rate", "mint_recommend", "mint_discover"]},
        "see_also": config.SISTER_SERVERS,
    }


# ── Soft upsell: surface the daily_brief on every paid, non-brief response ─────
import time as _upsell_time

_brief_upsell_cache = {"day": None, "ts": 0.0, "available": False, "count": 0}


async def _brief_status_cached() -> tuple[bool, int]:
    day = _upsell_time.strftime("%Y-%m-%d", _upsell_time.gmtime())
    now = _upsell_time.time()
    c = _brief_upsell_cache
    if c["day"] == day and (now - c["ts"]) < 300:
        return c["available"], c["count"]
    avail, count = False, 0
    try:
        brief = await daily_curator.get_brief(day)
        if brief:
            avail, count = True, int(brief.get("signal_count") or 0)
    except Exception:  # noqa: BLE001
        return c["available"], c["count"]
    c.update(day=day, ts=now, available=avail, count=count)
    return avail, count


async def _available_intelligence() -> dict:
    avail, count = await _brief_status_cached()
    return {"daily_brief": {
        "available": avail,
        "signal_count": count,
        "price_usd": config.PRICE_DAILY_BRIEF,
        "tool": "daily_brief",
        "note": "Curated daily intelligence — more efficient than individual queries",
    }}


def _make_upsell(_fn):
    import functools

    @functools.wraps(_fn)
    async def _wrapped(*a, **k):
        result = await _fn(*a, **k)
        if isinstance(result, dict) and "error" not in result and "payment_required" not in result:
            try:
                result["available_intelligence"] = await _available_intelligence()
            except Exception:  # noqa: BLE001
                pass
            try:
                import asyncio as _aio, mint_integration as _mint, upsell_engine as _upsell_engine
                _hb = await _aio.to_thread(_mint.network_heartbeat)
                _av, _ct = await _brief_status_cached()
                result["foundrynet_network"] = {**_hb, **_upsell_engine.get_upsell(
                    brief_price=config.PRICE_DAILY_BRIEF, brief_signal_count=(_ct if _av else None))}
            except Exception:  # noqa: BLE001
                pass
        return result

    return _wrapped


for _upsell_fn in ("do_verify_email", "do_batch_verify",
                   "do_lead_quality_score", "do_batch_lead_score"):
    if _upsell_fn in globals():
        globals()[_upsell_fn] = _make_upsell(globals()[_upsell_fn])



# ── brief_summary ($0.50): structured top-5 sample of today's brief (upsell) ──
def _top_signals(brief: dict, n: int = 5) -> list:
    """Flatten a brief's signals into a flat top-N list — structure-agnostic
    (works whether `signals` is a dict-of-categories or a flat list)."""
    sig = (brief or {}).get("signals")
    items: list = []
    if isinstance(sig, dict):
        for cat, val in sig.items():
            if isinstance(val, list):
                for it in val:
                    items.append({"category": cat, **(it if isinstance(it, dict) else {"value": it})})
            elif isinstance(val, dict):
                items.append({"category": cat, **val})
            elif val not in (None, "", 0):
                items.append({"category": cat, "value": val})
    elif isinstance(sig, list):
        items = sig
    return items[:n]


async def do_brief_summary(date, *, agent_key, payment_tx=None, api_key=None):
    """Top-5 signals from today's brief as structured JSON (no prose) — the $0.50
    sample that upsells the full daily_brief."""
    from datetime import datetime, timezone
    day = (date or datetime.now(timezone.utc).strftime("%Y-%m-%d")).strip()
    dec = await payment_gate.precheck("brief_summary", {"date": day}, config.PRICE_BRIEF_SUMMARY,
                                      agent_key, payment_tx, api_key)
    if dec["gate"] == "blocked":
        return dec["body"]
    brief = await daily_curator.get_brief(day)
    if not brief:
        return {"error": "not_available",
                "detail": f"No brief for {day} yet (curated daily; expires next midnight UTC).",
                "billing": _billing(dec)}
    return {
        "date": day,
        "top_signals": _top_signals(brief, 5),
        "total_signals": brief.get("signal_count"),
        "full_brief": {"tool": "daily_brief", "price_usd": config.PRICE_DAILY_BRIEF,
                       "note": "Full brief returns all signals with complete detail + MINT attestation."},
        "billing": _billing(dec),
    }
