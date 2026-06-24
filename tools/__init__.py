"""email-verify-mcp tools — one per file.

  verify_email   ($0.005)        deliverability + disposable/role/free + domain age
  batch_verify   ($0.003/email)  verify an array of emails (min $0.05)
  daily_brief    ($5)            curated daily verification-activity brief
  mint_info      (free)          FoundryNet Data Network + MINT cross-promo
"""
from . import verify_email as verify_email_tool
from . import batch_verify as batch_verify_tool
from . import lead_score as lead_score_tool
from . import daily_brief as daily_brief_tool
from . import brief_summary as brief_summary_tool
from . import mint as mint_tool


def register_all(mcp) -> None:
    verify_email_tool.register(mcp)
    batch_verify_tool.register(mcp)
    lead_score_tool.register(mcp)
    daily_brief_tool.register(mcp)
    brief_summary_tool.register(mcp)
    mint_tool.register(mcp)
