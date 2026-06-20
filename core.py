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


# ── daily_brief (premium, curated) ────────────────────────────────────────────
async def do_daily_brief(date, *, agent_key, payment_tx=None, api_key=None) -> dict:
    day = (date or datetime.now(timezone.utc).strftime("%Y-%m-%d")).strip()
    decision = await payment_gate.precheck("daily_brief", {"date": day},
                                           config.PRICE_DAILY_BRIEF, agent_key, payment_tx, api_key)
    if decision["gate"] == "blocked":
        return decision["body"]
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
                import asyncio as _aio, mint_integration as _mint
                result["foundrynet_network"] = await _aio.to_thread(_mint.network_heartbeat)
            except Exception:  # noqa: BLE001
                pass
        return result

    return _wrapped


for _upsell_fn in ("do_verify_email", "do_batch_verify"):
    if _upsell_fn in globals():
        globals()[_upsell_fn] = _make_upsell(globals()[_upsell_fn])
