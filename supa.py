"""Supabase PostgREST client for email-verify-mcp (standalone email-verify project).

Backs the per-domain verification cache (email_domain_cache), the free-tier counter
(ev_claim_free_query RPC), the x402 payment ledger (ev_payments), the daily brief
table (daily_briefs), and a lightweight verification log (ev_verify_log) used for
the daily brief stats. Every helper returns plain data and never raises.
"""
from __future__ import annotations

import logging
from typing import Optional

import config
from http_util import request_json

logger = logging.getLogger("ev.supa")


def configured() -> bool:
    return bool(config.SUPABASE_URL and config.SUPABASE_SERVICE_KEY)


def _headers(extra: Optional[dict] = None) -> dict:
    h = {"apikey": config.SUPABASE_SERVICE_KEY,
         "Authorization": f"Bearer {config.SUPABASE_SERVICE_KEY}",
         "Content-Type": "application/json", "Accept": "application/json"}
    # Shared-hub consolidation: target this service's namespaced schema via
    # PostgREST profile headers (Accept-Profile read / Content-Profile write).
    _sch = getattr(config, "SUPABASE_SCHEMA", "public")
    if _sch and _sch != "public":
        h["Accept-Profile"] = _sch
        h["Content-Profile"] = _sch
    if extra:
        h.update(extra)
    return h


def _url(path: str) -> str:
    return f"{config.SUPABASE_URL}/rest/v1/{path}"


async def _select(table: str, params: dict) -> list:
    if not configured():
        return []
    r = await request_json("GET", _url(table), headers=_headers(),
                           params=params, timeout=config.REQUEST_TIMEOUT)
    if isinstance(r, list):
        return r
    logger.warning(f"supa select {table} failed: {r}")
    return []


async def _rpc(fn: str, body: dict):
    if not configured():
        return None
    return await request_json("POST", _url(f"rpc/{fn}"), headers=_headers(),
                              body=body, timeout=config.REQUEST_TIMEOUT)


# ── email_domain_cache ────────────────────────────────────────────────────────
async def get_domain(domain: str) -> Optional[dict]:
    rows = await _select("email_domain_cache", {"domain": f"eq.{domain}", "select": "*", "limit": "1"})
    return rows[0] if rows else None


async def upsert_domain(row: dict) -> dict:
    if not configured():
        return {"error": "not_configured"}
    r = await request_json("POST", _url("email_domain_cache"),
                           headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                           params={"on_conflict": "domain"},
                           body=[row], timeout=config.REQUEST_TIMEOUT)
    if isinstance(r, list):
        return {"data": r}
    if isinstance(r, dict) and "error" not in r:
        return {"data": []}
    return r if isinstance(r, dict) else {"error": "bad_response", "detail": str(r)}


async def log_verification(domain: str, disposable: bool, deliverable) -> None:
    if not configured():
        return
    try:
        await request_json("POST", _url("ev_verify_log"),
                           headers=_headers({"Prefer": "return=minimal"}),
                           body={"domain": domain, "disposable": disposable,
                                 "deliverable": str(deliverable)},
                           timeout=config.REQUEST_TIMEOUT)
    except Exception:  # noqa: BLE001
        pass


# ── generic helpers (daily_curator) ──────────────────────────────────────────
async def select(table: str, params: dict) -> list:
    return await _select(table, params)


async def upsert(table: str, rows: list, on_conflict: str) -> dict:
    if not configured():
        return {"error": "not_configured"}
    r = await request_json("POST", _url(table),
                           headers=_headers({"Prefer": "resolution=merge-duplicates,return=representation"}),
                           params={"on_conflict": on_conflict},
                           body=rows, timeout=config.REQUEST_TIMEOUT)
    if isinstance(r, list):
        return {"data": r}
    return r if isinstance(r, dict) else {"error": "bad_response", "detail": str(r)}


async def rpc(fn: str, body: dict):
    return await _rpc(fn, body)


# ── free-tier counter ─────────────────────────────────────────────────────────
async def claim_free_query(agent_key: str, day: str, cap: int) -> Optional[dict]:
    r = await _rpc("ev_claim_free_query",
                   {"p_agent_key": agent_key, "p_day": day, "p_cap": cap})
    if isinstance(r, dict) and "allowed" in r:
        return r
    if isinstance(r, list) and r and isinstance(r[0], dict):
        return r[0]
    logger.warning(f"claim_free_query rpc unexpected: {r}")
    return None


# ── payment ledger ────────────────────────────────────────────────────────────
async def payment_tx_used(tx_signature: str) -> bool:
    rows = await _select("ev_payments",
                         {"tx_signature": f"eq.{tx_signature}", "select": "tx_signature", "limit": "1"})
    return bool(rows)


async def insert_payment(row: dict) -> dict:
    if not configured():
        return {"error": "not_configured"}
    r = await request_json("POST", _url("ev_payments"),
                           headers=_headers({"Prefer": "return=minimal"}),
                           body=row, timeout=config.REQUEST_TIMEOUT)
    if isinstance(r, list):
        return {"data": r}
    if isinstance(r, dict) and "error" not in r:
        return {"data": [r]}
    return r if isinstance(r, dict) else {"error": "bad_response", "detail": str(r)}
