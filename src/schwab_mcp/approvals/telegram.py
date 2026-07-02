from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Mapping, Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, User
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from schwab_mcp.approvals.base import (
    ApprovalDecision,
    ApprovalManager,
    ApprovalRequest,
)


logger = logging.getLogger(__name__)

_APPROVE = "approve"
_DENY = "deny"


@dataclass(slots=True, frozen=True)
class TelegramApprovalSettings:
    """Configuration values required for Telegram approvals."""

    token: str
    chat_id: int
    approver_ids: frozenset[int] = frozenset()
    timeout_seconds: float = 600.0


@dataclass(slots=True)
class _PendingApproval:
    request: ApprovalRequest
    future: asyncio.Future[ApprovalDecision]
    chat_id: int
    message_id: int


class TelegramApprovalManager(ApprovalManager):
    """Approval manager that routes decisions through a Telegram chat."""

    def __init__(self, settings: TelegramApprovalSettings) -> None:
        if not settings.approver_ids:
            raise ValueError(
                "TelegramApprovalManager requires at least one approver ID."
            )

        self._settings = settings
        self._application = Application.builder().token(settings.token).build()
        self._application.add_handler(CallbackQueryHandler(self._handle_callback))

        self._started = False
        self._start_lock = asyncio.Lock()
        self._pending: dict[str, _PendingApproval] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._start_lock:
            if self._started:
                return

            await self._application.initialize()
            await self._application.start()
            updater = self._application.updater
            if updater is not None:
                await updater.start_polling()
            self._started = True

    async def stop(self) -> None:
        async with self._start_lock:
            if not self._started:
                return

            updater = self._application.updater
            if updater is not None:
                await updater.stop()
            await self._application.stop()
            await self._application.shutdown()
            self._started = False

    async def require(self, request: ApprovalRequest) -> ApprovalDecision:
        await self.start()

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Approve", callback_data=f"{_APPROVE}:{request.id}"
                    ),
                    InlineKeyboardButton(
                        "❌ Deny", callback_data=f"{_DENY}:{request.id}"
                    ),
                ]
            ]
        )

        message = await self._application.bot.send_message(
            chat_id=self._settings.chat_id,
            text=self._build_pending_text(request),
            reply_markup=keyboard,
        )

        future: asyncio.Future[ApprovalDecision] = (
            asyncio.get_running_loop().create_future()
        )
        pending = _PendingApproval(
            request=request,
            future=future,
            chat_id=message.chat_id,
            message_id=message.message_id,
        )

        async with self._lock:
            self._pending[request.id] = pending

        try:
            decision = await asyncio.wait_for(
                future, timeout=self._settings.timeout_seconds
            )
        except asyncio.TimeoutError:
            decision = ApprovalDecision.EXPIRED
            await self._finalize_message(
                pending,
                decision,
                actor=None,
                reason=f"No decision within {int(self._settings.timeout_seconds)}s timeout.",
            )
        finally:
            async with self._lock:
                self._pending.pop(request.id, None)

        return decision

    async def _handle_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE  # noqa: ARG002
    ) -> None:
        query = update.callback_query
        if query is None or query.data is None:
            return

        message = query.message
        if message is None or message.chat.id != self._settings.chat_id:
            await query.answer()
            return

        action, _, request_id = query.data.partition(":")
        if action not in (_APPROVE, _DENY) or not request_id:
            await query.answer()
            return

        user = query.from_user
        if self._settings.approver_ids and (
            user is None or user.id not in self._settings.approver_ids
        ):
            logger.debug(
                "Ignoring callback from unauthorized user %s for request %s",
                user.id if user else None,
                request_id,
            )
            await query.answer(
                "You are not authorized to approve or deny this request.",
                show_alert=True,
            )
            return

        async with self._lock:
            pending = self._pending.get(request_id)

        if pending is None:
            await query.answer("This request is no longer pending.")
            return

        if pending.future.done():
            await query.answer()
            return

        decision = ApprovalDecision.APPROVED if action == _APPROVE else ApprovalDecision.DENIED
        await query.answer(f"Recorded: {decision.value}")

        await self._finalize_message(
            pending,
            decision,
            actor=user,
            reason="Decision recorded via Telegram button",
        )
        pending.future.set_result(decision)

    async def _finalize_message(
        self,
        pending: _PendingApproval,
        decision: ApprovalDecision,
        *,
        actor: User | None,
        reason: str | None,
    ) -> None:
        text = self._build_decision_text(
            pending.request, decision, actor=actor, reason=reason
        )
        try:
            await self._application.bot.edit_message_text(
                chat_id=pending.chat_id,
                message_id=pending.message_id,
                text=text,
            )
        except TelegramError:
            logger.warning(
                "Failed to update Telegram approval message for request %s",
                pending.request.id,
            )

    @staticmethod
    def _build_pending_text(request: ApprovalRequest) -> str:
        lines = [
            "⚠️ Write operation requires approval",
            f"🔧 Tool: {request.tool_name}",
            f"🆔 Request ID: {request.request_id}",
        ]
        if request.client_id:
            lines.append(f"💻 Client ID: {request.client_id}")
        if request.arguments:
            lines.append("Arguments: 📋")
            lines.append(TelegramApprovalManager._format_arguments(request.arguments))
        lines.append("")
        lines.append("👉 Tap a button below to approve or deny.")
        return "\n".join(lines)

    @staticmethod
    def _build_decision_text(
        request: ApprovalRequest,
        decision: ApprovalDecision,
        *,
        actor: User | None,
        reason: str | None,
    ) -> str:
        emoji = TelegramApprovalManager._emoji_for_decision(decision)
        lines = [
            f"{emoji} Write operation {decision.value}",
            f"🔧 Tool: {request.tool_name}",
            f"🆔 Request ID: {request.request_id}",
        ]
        if request.client_id:
            lines.append(f"💻 Client ID: {request.client_id}")
        if request.arguments:
            lines.append("Arguments: 📋")
            lines.append(TelegramApprovalManager._format_arguments(request.arguments))
        if actor is not None:
            lines.append(f"👤 Actor: {actor.full_name} (ID: {actor.id})")
        if reason:
            lines.append(f"📝 Notes: {reason}")
        return "\n".join(lines)

    # Argument keys that are internal identifiers, not useful for a human
    # reviewer to see (long opaque hashes) — omitted from the approval message.
    _HIDDEN_ARG_KEYS = frozenset({"account_hash"})

    # Best-effort emoji per well-known order-tool argument key. Falls back to
    # a plain bullet for anything not in this map.
    _ARG_KEY_EMOJI = {
        "symbol": "📈",
        "quantity": "🔢",
        "order_type": "🧾",
        "price": "💵",
        "stop_price": "🛑",
        "session": "🕒",
        "duration": "⏳",
        "trail_offset": "🎯",
        "trail_type": "🎯",
    }

    @staticmethod
    def _emoji_for_argument(key: str, value: str) -> str:
        if key == "instruction":
            upper = value.upper()
            if "SELL" in upper:
                return "🔴"
            if "BUY" in upper:
                return "🟢"
            return "↔️"
        return TelegramApprovalManager._ARG_KEY_EMOJI.get(key, "")

    @staticmethod
    def _format_arguments(arguments: Mapping[str, str]) -> str:
        visible = {
            key: value
            for key, value in arguments.items()
            if key not in TelegramApprovalManager._HIDDEN_ARG_KEYS
        }
        if not visible:
            return "<none>"

        lines = []
        for key, value in visible.items():
            emoji = TelegramApprovalManager._emoji_for_argument(key, value)
            suffix = f" {emoji}" if emoji else ""
            lines.append(f"  • {key} = {value}{suffix}")
        rendered = "\n".join(lines)
        if len(rendered) > 1000:
            return f"{rendered[:997]}..."
        return rendered

    @staticmethod
    def _emoji_for_decision(decision: ApprovalDecision) -> str:
        match decision:
            case ApprovalDecision.APPROVED:
                return "✅"
            case ApprovalDecision.DENIED:
                return "❌"
            case ApprovalDecision.EXPIRED:
                return "⌛"

    @staticmethod
    def authorized_user_ids(users: Sequence[int] | None) -> frozenset[int]:
        """Normalize a sequence of authorized Telegram user IDs."""
        if not users:
            return frozenset()
        return frozenset(int(user) for user in users)


__all__ = ["TelegramApprovalManager", "TelegramApprovalSettings"]
