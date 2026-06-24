"""Standard x402 compliance shim — makes this server discoverable on the x402
commerce directories (x402scan, 402 Index, CDP Bazaar) that our custom memo-based
rail (payment_gate.py) is invisible to.

Exposes probeable HTTP GET endpoints returning the standard x402 v2 schema (with
v1-compatible aliases + a PAYMENT-REQUIRED challenge header), pointing at the SAME
Solana wallet + USDC mint we already settle to. Prices resolve at runtime from
config, so repricing flows through automatically. Purely additive.
"""
from __future__ import annotations

import base64
import json

import config

SOLANA_CAIP2 = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
USDC_DECIMALS = 6
X402_VERSION = 2

SERVER_NAME = 'Email Verification MCP'
SERVER_DESC = 'B2B lead quality scoring — validate email, assess domain credibility, and score leads A-F before you invest outreach time. Also provides raw email deliverability and MX verification.'

# (tool, price-const-in-config | None, literal-fallback, human description)
_PAID = [
    ["brief_summary", "PRICE_BRIEF_SUMMARY", 0.5, "Brief summary — top 5 signals (sample of daily_brief)"],['verify_email', 'PRICE_VERIFY_EMAIL', 0.005, 'Verify email'], ['batch_verify', 'PRICE_BATCH_MIN', 0.01, 'Batch verify'], ['lead_quality_score', 'PRICE_LEAD_QUALITY', 0.01, 'B2B lead quality score (email + domain credibility, A-F)'], ['batch_lead_score', 'PRICE_BATCH_LEAD_MIN', 0.05, 'Batch B2B lead scoring (ranked)'], ['daily_brief', 'PRICE_DAILY_BRIEF', 5.0, 'Daily brief']]


def _price(const, fallback) -> float:
    if const:
        try:
            return float(getattr(config, const))
        except Exception:
            pass
    return float(fallback)


# tool -> (price_usdc, description), resolved at import from config.
PAID_TOOLS = {t: (_price(c, f), d) for (t, c, f, d) in _PAID if _price(c, f) > 0}


def _base_url() -> str:
    return config.PUBLIC_MCP_URL.rsplit("/mcp", 1)[0].rstrip("/")


def _atomic(price_usdc: float) -> str:
    return str(round(price_usdc * (10 ** USDC_DECIMALS)))


def resource_url(tool: str) -> str:
    return f"{_base_url()}/x402/{tool}"


def accepts_entry(tool: str, price_usdc: float, description: str) -> dict:
    amount = _atomic(price_usdc)
    return {
        "scheme": "exact",
        "network": SOLANA_CAIP2,
        "amount": amount,
        "maxAmountRequired": amount,
        "asset": config.PAYMENT_USDC_MINT,
        "payTo": config.PAYMENT_RECIPIENT,
        "resource": resource_url(tool),
        "description": description,
        "mimeType": "application/json",
        "maxTimeoutSeconds": getattr(config, "PAYMENT_EXPIRY_SECONDS", 300),
        "extra": {"feePayer": config.PAYMENT_RECIPIENT,
                  "networkName": "solana-mainnet", "assetSymbol": "USDC"},
        "outputSchema": {"input": {"type": "http", "method": "GET"},
                         "output": {"type": "application/json"}},
    }


def payment_required(tool: str) -> dict:
    price, description = PAID_TOOLS[tool]
    return {
        "x402Version": X402_VERSION,
        "error": "PAYMENT-SIGNATURE header is required",
        "resource": {"url": resource_url(tool),
                     "description": f"{SERVER_NAME} — {description}",
                     "mimeType": "application/json"},
        "accepts": [accepts_entry(tool, price, description)],
        "metadata": {"name": SERVER_NAME, "description": SERVER_DESC,
                     "network": "FoundryNet Data Network", "servers": 17,
                     "homepage": "https://foundrynet.io",
                     "mcp_endpoint": config.PUBLIC_MCP_URL,
                     "attestation": "MINT Protocol"},
        "extensions": {},
    }


def payment_required_header(tool: str) -> str:
    raw = json.dumps(payment_required(tool), separators=(",", ":")).encode()
    return base64.b64encode(raw).decode()


def index() -> dict:
    return {
        "x402Version": X402_VERSION, "name": SERVER_NAME, "description": SERVER_DESC,
        "network": "FoundryNet Data Network", "asset": config.PAYMENT_USDC_MINT,
        "chain": SOLANA_CAIP2, "payTo": config.PAYMENT_RECIPIENT,
        "resources": [{"tool": t, "url": resource_url(t), "price_usdc": p,
                       "amount": _atomic(p), "description": d}
                      for t, (p, d) in PAID_TOOLS.items()],
    }


def openapi() -> dict:
    """OpenAPI 3.1 doc for x402scan — one POST /x402/<tool> path per PAID tool, derived
    from PAID_TOOLS. x402scan requires POST + a requestBody on every path + a 402 on an
    empty-body probe; the /x402/<tool> route 402s for both GET and POST."""
    paths = {}
    for tool, (price, desc) in PAID_TOOLS.items():
        paths[f"/x402/{tool}"] = {
            "post": {
                "operationId": tool,
                "summary": desc,
                "x-x402-price": f"${price}",
                "requestBody": {
                    "required": False,
                    "content": {"application/json": {"schema": {"type": "object", "properties": {}}}},
                },
                "responses": {
                    "200": {"description": desc},
                    "402": {"description": "Payment required — x402 challenge"},
                },
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {"title": SERVER_NAME, "description": SERVER_DESC, "version": "1.0.0",
                 "contact": {"email": "foundrynet@proton.me"}},
        "servers": [{"url": _base_url()}],
        "paths": paths,
    }
