"""Normalized GitHub pull-request review state.

    import prstate
    sweep = prstate.fetch(owner="SakanaAIBusiness", author="@me")
    for pr in sweep.prs:
        pr.ci, pr.owed, pr.bot_findings
"""

from prstate.classify import classify
from prstate.gh import GhError, fetch
from prstate.model import (
    SCHEMA_VERSION,
    BotFinding,
    BotState,
    Check,
    Ci,
    CiState,
    Owed,
    PullRequest,
    Reason,
    ReasonKind,
    Surface,
    Sweep,
    ViewerReview,
)

__all__ = [
    "SCHEMA_VERSION",
    "BotFinding",
    "BotState",
    "Check",
    "Ci",
    "CiState",
    "GhError",
    "Owed",
    "PullRequest",
    "Reason",
    "ReasonKind",
    "Surface",
    "Sweep",
    "ViewerReview",
    "classify",
    "fetch",
]
