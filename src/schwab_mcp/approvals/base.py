"""Base classes and data types for the approval workflow."""

from __future__ import annotations

import abc
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum


class ApprovalDecision(str, Enum):
    """Decision returned by an approval workflow."""

    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


# Argument keys a reviewer may adjust during approval, with the type each
# override value is coerced to before the tool executes.
EDITABLE_ARGUMENT_TYPES: Mapping[str, type] = {
    "quantity": int,
    "price": float,
}


@dataclass(slots=True, frozen=True)
class ApprovalRequest:
    """Details about a write tool invocation requiring approval."""

    id: str
    tool_name: str
    request_id: str
    client_id: str | None
    arguments: Mapping[str, str]
    # Reviewer-supplied value overrides (raw strings, keyed per
    # EDITABLE_ARGUMENT_TYPES). Approval managers may fill this in before
    # resolving APPROVED; the write wrapper applies them to the actual call.
    overrides: dict[str, str] = field(default_factory=dict)


class ApprovalManager(abc.ABC):
    """Interface for asynchronous approval backends."""

    async def start(self) -> None:  # noqa: B027
        """Perform any startup/connection work."""

    async def stop(self) -> None:  # noqa: B027
        """Clean up resources."""

    @abc.abstractmethod
    async def require(self, request: ApprovalRequest) -> ApprovalDecision:
        """Require approval for the provided request."""


class NoOpApprovalManager(ApprovalManager):
    """Approval manager that always approves requests."""

    async def require(self, request: ApprovalRequest) -> ApprovalDecision:  # noqa: ARG002
        """Always approve the request without any external check."""
        return ApprovalDecision.APPROVED


__all__ = [
    "ApprovalDecision",
    "ApprovalManager",
    "ApprovalRequest",
    "EDITABLE_ARGUMENT_TYPES",
    "NoOpApprovalManager",
]
