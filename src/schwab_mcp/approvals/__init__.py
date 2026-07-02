"""Approval workflow backends for write-tool gating."""

from __future__ import annotations

from schwab_mcp.approvals.base import (
    ApprovalDecision,
    ApprovalManager,
    ApprovalRequest,
    NoOpApprovalManager,
)
from schwab_mcp.approvals.discord import (
    DiscordApprovalManager,
    DiscordApprovalSettings,
)
from schwab_mcp.approvals.telegram import (
    TelegramApprovalManager,
    TelegramApprovalSettings,
)

__all__ = [
    "ApprovalDecision",
    "ApprovalManager",
    "ApprovalRequest",
    "NoOpApprovalManager",
    "DiscordApprovalManager",
    "DiscordApprovalSettings",
    "TelegramApprovalManager",
    "TelegramApprovalSettings",
]
