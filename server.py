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
from starlette.middleware.base import BaseHTTPMiddleware
import event_log

import config
import core
import daily_curator
import identity
import payment_gate
import x402_standard
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


# ── okf-reliability-v1: emit reliability metadata on every tool result (#2964) ──
try:
    from okf_middleware import ReliabilityMiddleware
    mcp.add_middleware(ReliabilityMiddleware(server_id="email-verify"))
except Exception as _okf_e:  # noqa: BLE001
    import logging as _okf_log; _okf_log.getLogger(__name__).warning(f"okf middleware not wired: {_okf_e}")


@mcp.custom_route("/v1/reliability", methods=["GET"])
async def _okf_reliability_route(request):
    from starlette.responses import JSONResponse
    import okf_endpoint
    return JSONResponse(okf_endpoint.reliability_payload("email-verify"))


# ── Health ──────────────────────────────────────────────────────────────────
@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({
        "status": "ok", "service": "email-verify-mcp", "transport": "streamable-http",
        "network": "FoundryNet Data Network",
        "tools": ["verify_email", "batch_verify", "lead_quality_score",
                  "batch_lead_score", "daily_brief", "mint_info"],
        "cache": "supabase:email_domain_cache" if supa.configured() else "unconfigured",
        "cache_ttl_days": config.CACHE_TTL_DAYS,
        "smtp_check": "enabled" if config.SMTP_CHECK_ENABLED else "disabled",
        "x402_enabled": config.X402_ENABLED,
        "query_payment": "armed" if payment_gate.is_active() else "free",
        "prices_usdc": {"verify_email": config.PRICE_VERIFY_EMAIL,
                        "batch_per_email": config.PRICE_BATCH_PER_EMAIL,
                        "batch_min": config.PRICE_BATCH_MIN,
                        "lead_quality_score": config.PRICE_LEAD_QUALITY,
                        "batch_lead_per_email": config.PRICE_LEAD_PER_EMAIL,
                        "batch_lead_min": config.PRICE_BATCH_LEAD_MIN,
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


@mcp.custom_route("/v1/lead-quality-score", methods=["POST"])
async def rest_lead_score(request: Request) -> JSONResponse:
    b = await _json_body(request)
    return _resp(await core.do_lead_quality_score(b.get("email", ""), b.get("domain"),
                                                  agent_key=_akey(request, b),
                                                  payment_tx=b.get("payment_tx"), api_key=identity.bearer(request)))


@mcp.custom_route("/v1/batch-lead-score", methods=["POST"])
async def rest_batch_lead_score(request: Request) -> JSONResponse:
    b = await _json_body(request)
    return _resp(await core.do_batch_lead_score(b.get("leads", []), agent_key=_akey(request, b),
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
    "name": "B2B Lead Quality Scorer MCP",
    "description": ("B2B lead quality scoring — validate email, assess domain "
                    "credibility, and score leads A-F before you invest outreach time. "
                    "Also provides raw email deliverability and MX verification."),
    "url": config.PUBLIC_MCP_URL,
    "version": "1.0.0",
    "capabilities": {"tools": ["lead_quality_score", "batch_lead_score",
                               "verify_email", "batch_verify", "daily_brief", "mint_info"]},
    "provider": {"name": "FoundryNet", "url": "https://foundrynet.io"},
    "network": "FoundryNet Data Network",
    "attestation": {"protocol": "MINT Protocol",
                    "endpoint": "https://mint-mcp-production.up.railway.app/mcp",
                    "verified_outputs": True, "live_feed": "https://mint.foundrynet.io/feed", "feed_api": "https://mint-mcp-production.up.railway.app/v1/feed"},
    "protocols": {"mcp": {"endpoint": config.PUBLIC_MCP_URL, "transport": "streamable-http", "tools_count": 6},
                  "x402": {"supported": True, "currency": "USDC", "network": "solana"}},
    "contact": "forge@foundrynet.io",
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
        "tools": live, "version": "1.0", "name": "B2B Lead Quality Scorer MCP",
        "tagline": "Score B2B leads A-F before you invest outreach time.",
        "description": ("B2B lead quality scoring — validate email, assess domain "
                        "credibility, and score leads A-F before you invest outreach time. "
                        "Also provides raw email deliverability and MX verification: "
                        "disposable/role/free detection, MX validity, domain age, and SMTP "
                        "checks. A free-tier alternative to ZeroBounce/NeverBounce/Hunter, "
                        "with cross-network domain enrichment via x402."),
        "serverUrl": config.PUBLIC_MCP_URL, "transport": "streamable-http",
        "tools_count": len(live),
        "categories": ["data", "enrichment", "email", "sales", "verification"],
        "keywords": ["lead-scoring", "sales-prospecting", "email-quality", "b2b-leads",
                     "outreach-validation", "email verification", "deliverability",
                     "disposable email", "lead enrichment", "list hygiene", "mx lookup"],
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



# ── Standard x402 compliance (discoverable on x402scan / 402 Index / CDP Bazaar) ──
@mcp.custom_route("/x402", methods=["GET"])
async def x402_index(request: Request) -> JSONResponse:
    return JSONResponse(x402_standard.index(),
                        headers={"Cache-Control": "public, max-age=300",
                                 "Access-Control-Allow-Origin": "*"})


@mcp.custom_route("/.well-known/x402", methods=["GET"])
async def x402_wellknown(request: Request) -> JSONResponse:
    return JSONResponse(x402_standard.index(),
                        headers={"Cache-Control": "public, max-age=300",
                                 "Access-Control-Allow-Origin": "*"})


@mcp.custom_route("/x402/{tool}", methods=["GET", "POST"])
async def x402_resource(request: Request) -> JSONResponse:
    tool = request.path_params["tool"]
    if tool not in x402_standard.PAID_TOOLS:
        return JSONResponse({"error": "unknown_resource", "tool": tool,
                             "available": list(x402_standard.PAID_TOOLS)}, status_code=404)
    challenge = x402_standard.payment_required_header(tool)
    return JSONResponse(x402_standard.payment_required(tool), status_code=402,
                        headers={"Cache-Control": "public, max-age=300",
                                 "Access-Control-Allow-Origin": "*",
                                 "PAYMENT-REQUIRED": challenge,
                                 "X-PAYMENT": challenge,
                                 "Link": '</openapi.json>; rel="describedby"',
                                 "WWW-Authenticate": 'x402 version="2"'})


@mcp.custom_route("/openapi.json", methods=["GET"])
async def openapi_doc(request: Request) -> JSONResponse:
    """OpenAPI 3.1 discovery doc — x402scan requires a spec at a discoverable URL."""
    return JSONResponse(x402_standard.openapi(),
                        headers={"Cache-Control": "public, max-age=300",
                                 "Access-Control-Allow-Origin": "*",
                                 "Link": '</openapi.json>; rel="describedby"'})


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
    # Per-call telemetry middleware (fire-and-forget to agents ingest).
    main_app.add_middleware(BaseHTTPMiddleware, dispatch=event_log.middleware)
    return main_app


if __name__ == "__main__":
    import uvicorn
    logger.info(f"email-verify-mcp starting on 0.0.0.0:{config.PORT} "
                f"(cache={'supabase' if supa.configured() else 'off'}, x402={config.X402_ENABLED})")
    uvicorn.run(build_dual_app(), host="0.0.0.0", port=config.PORT, log_level="warning")
