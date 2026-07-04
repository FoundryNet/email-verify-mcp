# B2B Lead Quality Scorer MCP

Part of the **FoundryNet Data Network**.

B2B lead quality scoring — validate email, assess domain credibility, and score
leads **A-F** before you invest outreach time. Also provides raw email
deliverability and MX verification.

## Tools

| Tool | Price | What it does |
| --- | --- | --- |
| `lead_quality_score` | $0.01/lead | Score one B2B lead 0-100 (A-F) from email deliverability + domain credibility, with signals + a recommendation |
| `batch_lead_score` | $0.01/lead, min $0.05 | Score a batch of leads, ranked best-first, with a grade distribution |
| `verify_email` | $0.005 | Single-address deliverability + quality signals (MX, disposable, role, free, domain age) |
| `batch_verify` | $0.003/email, min $0.05 | Verify an array of addresses (volume play) |
| `daily_brief` | $5 | Curated daily verification-activity brief |
| `mint_info` | free | FoundryNet Data Network + MINT cross-promo |

Free tier 25 queries/day per agent, then x402 (USDC on Solana). An `fnet_` Bearer
key bypasses. Transport: Streamable HTTP at `/mcp` (+ legacy `/sse`).

## Live network activity

**Live feed:** [mint.foundrynet.io/feed](https://mint.foundrynet.io/feed)  
Real-time verified work across 21 servers and autonomous agents, anchored on Solana via [MINT Protocol](https://mint.foundrynet.io).
