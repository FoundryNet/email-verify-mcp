from typing import List, Optional

import core
import identity


def register(mcp) -> None:
    @mcp.tool
    async def lead_quality_score(
        email: str,
        domain: Optional[str] = None,
        agent_id: Optional[str] = None,
        payment_tx: Optional[str] = None,
    ) -> dict:
        """Score a B2B lead 0-100 (grade A-F) from email deliverability + domain
        credibility — decide whether a lead is worth your outreach time before you
        spend it. Combines deliverable/MX validity, disposable & role-account & free-
        provider detection, and domain age (cross-enriched from the FoundryNet brand-
        intel network) into a single score, with named signals and a recommendation.

        PAID: $0.01 USDC per lead after the daily free allowance (25/day). On a 402,
        pay the returned Solana memo and re-call with the SAME args plus
        payment_tx=<signature>. An Authorization: Bearer fnet_ key bypasses payment.

        Args:
            email: the lead's email address.
            domain: optional company domain (defaults to the email's domain).
            agent_id: stable id for your agent (scopes the free-tier counter).
            payment_tx: Solana tx signature, when re-calling after a 402.
        """
        return await core.do_lead_quality_score(
            email, domain, agent_key=identity.resolve_agent_key(agent_id),
            payment_tx=payment_tx, api_key=identity.bearer())

    @mcp.tool
    async def batch_lead_score(
        leads: List[str],
        agent_id: Optional[str] = None,
        payment_tx: Optional[str] = None,
    ) -> dict:
        """Score a batch of B2B leads in one call and get them back ranked best-first
        — the volume play for prioritizing a prospect list. Each lead is scored 0-100
        (A-F) on email deliverability + domain credibility; returns a ranked result
        array plus a grade distribution and average score. Up to 100 leads per call.

        PAID: $0.01 USDC per lead, minimum $0.05, after the daily free allowance. On a
        402, pay the returned Solana memo and re-call with the SAME args plus
        payment_tx=<signature>. An Authorization: Bearer fnet_ key bypasses payment.

        Args:
            leads: list of email addresses (max 100; deduped).
            agent_id: stable id for your agent (scopes the free-tier counter).
            payment_tx: Solana tx signature, when re-calling after a 402.
        """
        return await core.do_batch_lead_score(
            leads, agent_key=identity.resolve_agent_key(agent_id),
            payment_tx=payment_tx, api_key=identity.bearer())
