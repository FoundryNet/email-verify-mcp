"""email-verify-mcp — email & contact verification for autonomous agents.

A FastMCP server with a per-domain on-demand cache over its OWN standalone Supabase
project. Each query resolves an email to deliverability signals (MX validity,
disposable/role/free detection, domain age, best-effort SMTP probe) and caches the
domain-level facts for 7 days. No cron — pure on-demand with caching. A free-tier
alternative to enterprise email verification (ZeroBounce, NeverBounce, Hunter).

  verify_email   — single-address deliverability + quality signals  ($0.005)
  batch_verify   — array of addresses, the volume play              ($0.003/email, min $0.05)
  daily_brief    — curated daily verification-activity brief         ($5)
  mint_info      — FoundryNet Data Network + MINT cross-promo        (free)

Free tier 25 queries/day per agent, then x402 (USDC on Solana). Bearer fnet_ key
bypasses. Transport: Streamable HTTP at /mcp (+ legacy /sse). Health: /health.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

import config
import core
import daily_curator
import identity
import payment_gate
import supa
import tools

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ev.mcp")

if not supa.configured():
    logger.warning("SUPABASE_SERVICE_KEY not set — cache disabled; verification still "
                   "works but nothing is persisted.")

mcp = FastMCP("email-verify")

if payment_gate.is_active():
    logger.info(f"pay-per-query ARMED → {config.PAYMENT_RECIPIENT} after "
                f"{config.FREE_TIER_DAILY}/day free (verify=${config.PRICE_VERIFY_EMAIL}, "
                f"batch=${config.PRICE_BATCH_PER_EMAIL}/email)")
else:
    logger.info("pay-per-query INERT (X402 off or recipient unset) — all tools free")

tools.register_all(mcp)


# ── Health ──────────────────────────────────────────────────────────────────
@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({
        "status": "ok", "service": "email-verify-mcp", "transport": "streamable-http",
        "network": "FoundryNet Data Network",
        "tools": ["verify_email", "batch_verify", "daily_brief", "mint_info"],
        "cache": "supabase:email_domain_cache" if supa.configured() else "unconfigured",
        "cache_ttl_days": config.CACHE_TTL_DAYS,
        "smtp_check": "enabled" if config.SMTP_CHECK_ENABLED else "disabled",
        "x402_enabled": config.X402_ENABLED,
        "query_payment": "armed" if payment_gate.is_active() else "free",
        "prices_usdc": {"verify_email": config.PRICE_VERIFY_EMAIL,
                        "batch_per_email": config.PRICE_BATCH_PER_EMAIL,
                        "batch_min": config.PRICE_BATCH_MIN,
                        "daily_brief": config.PRICE_DAILY_BRIEF},
        "free_tier_daily": config.FREE_TIER_DAILY,
        "payment_recipient": config.PAYMENT_RECIPIENT,
    })


@mcp.custom_route("/ping", methods=["GET"])
async def ping(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


# ── REST surface ─────────────────────────────────────────────────────────────
_ERR_STATUS = {"bad_request": 400, "not_configured": 503, "not_found": 404,
               "payment_required": 402, "not_available": 404}


def _resp(d: dict) -> JSONResponse:
    if "error" not in d:
        return JSONResponse(d, status_code=200)
    err = str(d.get("error") or "")
    code = _ERR_STATUS.get(err, 502 if err in ("network", "non_json_response", "unreachable") else 400)
    if err.startswith("http_") and err[5:].isdigit():
        code = int(err[5:])
    return JSONResponse(d, status_code=code)


async def _json_body(request: Request) -> dict:
    try:
        b = await request.json()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


def _akey(request: Request, body: dict) -> str:
    return identity.resolve_agent_key(body.get("agent_id"), request=request)


@mcp.custom_route("/v1/verify", methods=["POST"])
async def rest_verify(request: Request) -> JSONResponse:
    b = await _json_body(request)
    return _resp(await core.do_verify_email(b.get("email", ""), agent_key=_akey(request, b),
                                            payment_tx=b.get("payment_tx"), api_key=identity.bearer(request)))


@mcp.custom_route("/v1/batch-verify", methods=["POST"])
async def rest_batch(request: Request) -> JSONResponse:
    b = await _json_body(request)
    return _resp(await core.do_batch_verify(b.get("emails", []), agent_key=_akey(request, b),
                                            payment_tx=b.get("payment_tx"), api_key=identity.bearer(request)))


@mcp.custom_route("/v1/daily-brief", methods=["POST"])
async def rest_daily_brief(request: Request) -> JSONResponse:
    b = await _json_body(request)
    return _resp(await core.do_daily_brief(b.get("date"), agent_key=_akey(request, b),
                                           payment_tx=b.get("payment_tx"), api_key=identity.bearer(request)))


@mcp.custom_route("/v1/mint-info", methods=["GET", "POST"])
async def rest_mint(request: Request) -> JSONResponse:
    return JSONResponse(core.mint_info())


# ── Discovery ────────────────────────────────────────────────────────────────
_AGENT_CARD = {
    "name": "Email Verification MCP",
    "description": ("Verify email addresses on demand — deliverability, disposable/role/"
                    "free detection, MX validity, and domain age — for lead enrichment, "
                    "signup gating, and list hygiene."),
    "url": config.PUBLIC_MCP_URL,
    "version": "1.0.0",
    "capabilities": {"tools": ["verify_email", "batch_verify", "daily_brief", "mint_info"]},
    "provider": {"name": "FoundryNet", "url": "https://foundrynet.io"},
    "network": "FoundryNet Data Network",
    "attestation": {"protocol": "MINT Protocol",
                    "endpoint": "https://mint-mcp-production.up.railway.app/mcp",
                    "verified_outputs": True, "live_feed": "https://mint.foundrynet.io/feed", "feed_api": "https://mint-mcp-production.up.railway.app/v1/feed"},
    "protocols": {"mcp": {"endpoint": config.PUBLIC_MCP_URL, "transport": "streamable-http", "tools_count": 4},
                  "x402": {"supported": True, "currency": "USDC", "network": "solana"}},
    "contact": "hello@foundrynet.io",
}


@mcp.custom_route("/.well-known/agent-card.json", methods=["GET"])
async def agent_card(request: Request) -> JSONResponse:
    return JSONResponse(_AGENT_CARD, headers={"Cache-Control": "public, max-age=300"})


@mcp.custom_route("/.well-known/mcp", methods=["GET"])
async def mcp_endpoints(request: Request) -> JSONResponse:
    return JSONResponse({"endpoints": [{"url": config.PUBLIC_MCP_URL,
                                        "transport": "streamable-http",
                                        "name": "Email Verification MCP"}]},
                        headers={"Cache-Control": "public, max-age=300"})


async def _live_tools() -> list:
    res = mcp.list_tools()
    if inspect.iscoroutine(res):
        res = await res
    return [{"name": t.name, "description": (getattr(t, "description", "") or "").strip(),
             "inputSchema": getattr(t, "parameters", None) or {"type": "object"}} for t in res]


@mcp.custom_route("/.well-known/mcp/server-card.json", methods=["GET"])
async def server_card(request: Request) -> JSONResponse:
    live = await _live_tools()
    return JSONResponse({
        "serverInfo": {"name": "Email Verification MCP", "version": "1.0.0"},
        "authentication": {"type": "http", "scheme": "bearer",
                           "description": ("mint_info is free; verify tools give 25 free "
                                           "queries/day then take an fnet_ Bearer key OR x402 USDC.")},
        "tools": live, "version": "1.0", "name": "Email Verification MCP",
        "tagline": "Email deliverability & contact verification for agents.",
        "description": ("Email & contact verification: deliverability, disposable/role/free "
                        "detection, MX validity, domain age, and SMTP checks. A free-tier "
                        "alternative to ZeroBounce/NeverBounce/Hunter — half a cent per check "
                        "via x402, with a daily free tier."),
        "serverUrl": config.PUBLIC_MCP_URL, "transport": "streamable-http",
        "tools_count": len(live),
        "categories": ["data", "enrichment", "email", "sales", "verification"],
        "keywords": ["email verification", "deliverability", "disposable email",
                     "lead enrichment", "list hygiene", "mx lookup"],
        "network": "FoundryNet Data Network", "see_also": config.SISTER_SERVERS,
        "pricing": {"model": "metered",
                    "free_tier": f"{config.FREE_TIER_DAILY} queries/day per agent",
                    "paid_from": f"{config.PRICE_VERIFY_EMAIL} USDC per query (x402)"},
    }, headers={"Cache-Control": "public, max-age=300"})


# ── Entrypoint ───────────────────────────────────────────────────────────────
_FREE_TOOL_NAMES = {"mint_info", "macro_dashboard", "cve_detail", "detail",
                    "domain_age", "convert", "rates", "market_overview", "price",
                    "quote", "batch_quote", "sector_performance"}


@mcp.custom_route("/.well-known/mcp.json", methods=["GET"])
async def wellknown_mcp_json(request: Request) -> JSONResponse:
    """Machine-discovery card (emerging standard) for AI clients/crawlers."""
    live = await _live_tools()
    names = [t["name"] for t in live]
    return JSONResponse({
        "name": _AGENT_CARD["name"],
        "description": _AGENT_CARD["description"],
        "url": config.PUBLIC_MCP_URL,
        "transport": ["streamable-http"],
        "tools": names,
        "pricing": {"model": "per-query", "free_tier": True,
                    "paid_tools": [n for n in names if n not in _FREE_TOOL_NAMES]},
        "attestation": {"enabled": True, "protocol": "MINT Protocol",
                        "feed": "https://mint.foundrynet.io/feed"},
        "network": {"name": "FoundryNet Data Network", "servers": 17,
                    "homepage": "https://foundrynet.io"},
    }, headers={"Cache-Control": "public, max-age=300"})


def build_dual_app():
    main_app = mcp.http_app(transport="http", path="/mcp")
    sse_app = mcp.http_app(transport="sse", path="/sse")
    for r in sse_app.routes:
        if getattr(r, "path", None) in ("/sse", "/messages"):
            main_app.router.routes.append(r)
    main_life, sse_life = main_app.router.lifespan_context, sse_app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def _dual_lifespan(app):
        async with main_life(app):
            async with sse_life(app):
                brief_task = asyncio.create_task(daily_curator.curator_loop())
                try:
                    yield
                finally:
                    brief_task.cancel()
                    with contextlib.suppress(Exception):
                        await brief_task
    main_app.router.lifespan_context = _dual_lifespan
    return main_app


if __name__ == "__main__":
    import uvicorn
    logger.info(f"email-verify-mcp starting on 0.0.0.0:{config.PORT} "
                f"(cache={'supabase' if supa.configured() else 'off'}, x402={config.X402_ENABLED})")
    uvicorn.run(build_dual_app(), host="0.0.0.0", port=config.PORT, log_level="warning")
