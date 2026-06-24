from typing import Optional

import core
import identity


def register(mcp) -> None:
    @mcp.tool
    async def daily_brief(
        date: Optional[str] = None,
        agent_id: Optional[str] = None,
        payment_tx: Optional[str] = None,
        stripe_token: Optional[str] = None,
    ) -> dict:
        """Get the curated daily email-verification brief — the day's verification
        activity in one package: volume, deliverability mix, disposable-domain hit
        rate, and the most-queried + most-flagged domains. Each brief carries a MINT
        provenance attestation so a buyer can verify it was produced by this server,
        unaltered.

        PAID: $5 USDC per brief. Defaults to today (UTC); a brief expires at the next
        midnight UTC. On a 402, pay the returned Solana memo and re-call with the SAME
        args plus payment_tx=<signature>. An fnet_ Bearer key bypasses payment.

        Args:
            date: brief date YYYY-MM-DD (default today, UTC).
            agent_id: stable id for your agent (scopes the free-tier counter).
            payment_tx: Solana tx signature, when re-calling after a 402.
            stripe_token: Stripe Checkout Session id (cs_…), when re-calling after
                paying the Stripe payment link (alternative to x402). Can also be
                supplied via the X-Stripe-Token header.
        """
        return await core.do_daily_brief(date, agent_key=identity.resolve_agent_key(agent_id),
                                         payment_tx=payment_tx, api_key=identity.bearer(),
                                         stripe_token=stripe_token or identity.stripe_token())
