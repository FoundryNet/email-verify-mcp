"""Daily curated brief — email-verify.

Runs once a day at BRIEF_HOUR_UTC (05:00 UTC) as an in-process background task.
It summarizes the last 24h of verification activity (volume, deliverability mix,
disposable-domain hits, most-queried domains), attests the package through MINT,
and upserts it into `daily_briefs`. The paid `daily_brief` tool reads that row.
This is an on-demand service, so the brief reflects observed traffic.
"""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

import config
import mint_integration
import supa

logger = logging.getLogger("ev.curator")

SERVER = config.SERVER_SLUG
PRICE = config.PRICE_DAILY_BRIEF


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _expires_at(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (d + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")


def related_briefs(exclude: str) -> list:
    return [{"server": s, "price": p, "tool": "daily_brief"}
            for s, p in config.NETWORK_BRIEFS.items() if s != exclude]


async def _curate_signals(since_iso: str) -> tuple[dict, int]:
    rows = await supa.select("ev_verify_log", {
        "select": "domain,disposable,deliverable,created_at",
        "created_at": f"gte.{since_iso}", "order": "created_at.desc", "limit": "5000"})
    total = len(rows)
    disposable = sum(1 for r in rows if r.get("disposable"))
    deliverable = sum(1 for r in rows if str(r.get("deliverable")) == "True")
    undeliverable = sum(1 for r in rows if str(r.get("deliverable")) == "False")
    domain_counts = Counter(r.get("domain") for r in rows if r.get("domain"))
    disposable_counts = Counter(r.get("domain") for r in rows
                                if r.get("disposable") and r.get("domain"))

    signals = {
        "activity_summary": {
            "verifications_24h": total,
            "deliverable": deliverable,
            "undeliverable": undeliverable,
            "unknown": total - deliverable - undeliverable,
            "disposable_hits": disposable,
            "disposable_rate_pct": round(100 * disposable / total, 1) if total else 0,
        },
        "top_queried_domains": [{"domain": d, "count": n}
                                for d, n in domain_counts.most_common(15)],
        "top_disposable_domains": [{"domain": d, "count": n}
                                   for d, n in disposable_counts.most_common(10)],
    }
    count = total
    return signals, count


async def run_curation(date_str: str | None = None) -> dict:
    date_str = date_str or _today()
    since_iso = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
    signals, count = await _curate_signals(since_iso)

    brief = {
        "brief_date": date_str, "server": SERVER, "signal_count": count,
        "signals": signals, "expires_at": _expires_at(date_str),
        "related_briefs": related_briefs(SERVER),
    }
    attestation = await asyncio.to_thread(
        mint_integration.attest_data, brief, "analysis",
        f"Daily {SERVER} brief: {count} verifications summarized")
    brief["provenance"] = attestation

    row = {
        "brief_date": date_str, "brief_data": brief, "signal_count": count,
        "attestation_hash": attestation.get("attestation_hash"),
        "expires_at": _expires_at(date_str),
    }
    res = await supa.upsert("daily_briefs", [row], "brief_date")
    if isinstance(res, dict) and res.get("error"):
        logger.warning(f"daily brief upsert failed: {str(res)[:200]}")
    else:
        logger.info(f"daily brief stored: {date_str} ({count} verifications)")
    return brief


async def get_brief(date_str: str | None = None) -> dict | None:
    date_str = date_str or _today()
    rows = await supa.select("daily_briefs",
                             {"select": "*", "brief_date": f"eq.{date_str}", "limit": "1"})
    if not rows:
        return None
    row = rows[0]
    exp = row.get("expires_at")
    if exp:
        try:
            if datetime.now(timezone.utc) >= datetime.fromisoformat(exp.replace("Z", "+00:00")):
                return None
        except Exception:  # noqa: BLE001
            pass
    return row.get("brief_data")


async def bump_purchase(date_str: str) -> None:
    try:
        await supa.rpc("increment_brief_purchase", {"p_brief_date": date_str})
    except Exception:  # noqa: BLE001
        pass


async def curator_loop() -> None:
    while True:
        now = datetime.now(timezone.utc)
        secs = now.hour * 3600 + now.minute * 60 + now.second
        wait = (config.BRIEF_HOUR_UTC * 3600 - secs) % 86400 or 86400
        try:
            await asyncio.sleep(wait)
            if supa.configured():
                await run_curation()
        except asyncio.CancelledError:
            break
        except Exception as e:  # noqa: BLE001
            logger.warning(f"curator loop error: {e}")
            await asyncio.sleep(3600)
