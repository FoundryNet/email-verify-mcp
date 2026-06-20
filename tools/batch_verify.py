from typing import List, Optional

import core
import identity


def register(mcp) -> None:
    @mcp.tool
    async def batch_verify(
        emails: List[str],
        agent_id: Optional[str] = None,
        payment_tx: Optional[str] = None,
    ) -> dict:
        """Verify an array of email addresses in one call — the volume play for list
        hygiene and bulk lead enrichment. Returns a per-email result array (same
        signals as verify_email) plus a deliverable/disposable summary. Up to 100
        emails per call; domain-level facts are cached and deduped, so repeat domains
        are cheap.

        PAID: $0.003 USDC per email, minimum $0.05, after the daily free allowance.
        On a 402, pay the returned Solana memo and re-call with the SAME args plus
        payment_tx=<signature>. An Authorization: Bearer fnet_ key bypasses payment.

        Args:
            emails: list of email addresses (max 100; deduped).
            agent_id: stable id for your agent (scopes the free-tier counter).
            payment_tx: Solana tx signature, when re-calling after a 402.
        """
        return await core.do_batch_verify(emails, agent_key=identity.resolve_agent_key(agent_id),
                                          payment_tx=payment_tx, api_key=identity.bearer())
