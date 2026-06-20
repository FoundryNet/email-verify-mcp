from typing import Optional

import core
import identity


def register(mcp) -> None:
    @mcp.tool
    async def verify_email(
        email: str,
        agent_id: Optional[str] = None,
        payment_tx: Optional[str] = None,
    ) -> dict:
        """Verify a single email address — deliverability and quality signals for
        lead enrichment, signup gating, and list hygiene. Returns deliverable
        (true/false/unknown), mx_valid, disposable (throwaway/temp-mail), role_account
        (info@, support@, …), free_provider (gmail, yahoo, …), domain_age_days, and a
        best-effort smtp_check. Domain-level facts are cached 7 days.

        PAID: $0.005 USDC per query after a daily free allowance (25/day). On a 402,
        pay the returned Solana memo and re-call with the SAME args plus
        payment_tx=<signature>. An Authorization: Bearer fnet_ key bypasses payment.

        Args:
            email: the email address to verify.
            agent_id: stable id for your agent (scopes the free-tier counter).
            payment_tx: Solana tx signature, when re-calling after a 402.
        """
        return await core.do_verify_email(email, agent_key=identity.resolve_agent_key(agent_id),
                                          payment_tx=payment_tx, api_key=identity.bearer())
