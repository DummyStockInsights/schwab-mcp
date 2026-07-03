from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass
from typing import Mapping, Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, User
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from schwab_mcp.approvals.base import (
    ApprovalDecision,
    ApprovalManager,
    ApprovalRequest,
)


logger = logging.getLogger(__name__)

_APPROVE = "approve"
_DENY = "deny"

# Matches edit commands in replies, e.g. "qty 5", "quantity=5", "price: 1.95",
# "数量 5 价格 1.95". Aliases map onto the canonical argument keys.
_EDIT_PATTERN = re.compile(
    r"(quantity|qty|price|数量|价格)\s*[=:]?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_EDIT_KEY_ALIASES = {
    "qty": "quantity",
    "quantity": "quantity",
    "数量": "quantity",
    "price": "price",
    "价格": "price",
}


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
        self._application.add_handler(
            MessageHandler(filters.TEXT & filters.REPLY, self._handle_reply)
        )

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

    @staticmethod
    def _build_keyboard(request_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Approve", callback_data=f"{_APPROVE}:{request_id}"
                    ),
                    InlineKeyboardButton(
                        "❌ Deny", callback_data=f"{_DENY}:{request_id}"
                    ),
                ]
            ]
        )

    async def require(self, request: ApprovalRequest) -> ApprovalDecision:
        await self.start()

        message = await self._application.bot.send_message(
            chat_id=self._settings.chat_id,
            text=self._build_pending_text(request),
            reply_markup=self._build_keyboard(request.id),
            parse_mode="HTML",
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

    async def _handle_reply(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE  # noqa: ARG002
    ) -> None:
        message = update.message
        if message is None or message.chat.id != self._settings.chat_id:
            return
        reply_to = message.reply_to_message
        if reply_to is None:
            return

        user = message.from_user
        if self._settings.approver_ids and (
            user is None or user.id not in self._settings.approver_ids
        ):
            return

        async with self._lock:
            pending = next(
                (
                    p
                    for p in self._pending.values()
                    if p.message_id == reply_to.message_id
                ),
                None,
            )
        if pending is None:
            return  # not a pending approval message (or already decided)

        edits = self._parse_edit_text(message.text or "")
        if not edits:
            await message.reply_text(
                 '无法识别修改指令。示例: "qty 5"、"price 1.95"、"qty 5 price 1.95"'
            )
            return

        applied: list[str] = []
        rejected: list[str] = []
        for key, raw in edits.items():
            ok, why = self._validate_override(pending.request, key, raw)
            if ok:
                original = pending.request.arguments.get(key, "?")
                notes = self._record_override(pending.request, key, raw)
                applied.append(f"{key}: {original} → {raw}" + notes)
            else:
                rejected.append(f"{key}: {why}")

        if applied:
            await self._refresh_pending_message(pending)

        lines = []
        if applied:
            lines.append("✏️ 已更新 " + ", ".join(applied))
        if rejected:
            lines.append("⚠️ 未采纳 " + "; ".join(rejected))
        await message.reply_text("\n".join(lines))

    @staticmethod
    def _parse_edit_text(text: str) -> dict[str, str]:
        edits: dict[str, str] = {}
        for alias, value in _EDIT_PATTERN.findall(text):
            key = _EDIT_KEY_ALIASES.get(alias.lower())
            if key:
                edits[key] = value
        return edits

    @staticmethod
    def _validate_override(
        request: ApprovalRequest, key: str, raw: str
    ) -> tuple[bool, str]:
        if key not in ("quantity", "price"):
            return False, "not editable"
        if key not in request.arguments:
            return False, "not part of this order"
        if key == "quantity":
            try:
                quantity = int(raw)
            except ValueError:
                return False, "quantity must be a whole number"
            if quantity <= 0:
                return False, "quantity must be positive"
        elif key == "price":
            try:
                price = float(raw)
            except ValueError:
                return False, "price must be a number"
            if price <= 0:
                return False, "price must be positive"
        return True, ""

    @staticmethod
    def _record_override(request: ApprovalRequest, key: str, raw: str) -> str:
        """Record a validated override. Returns extra notes for the reply.

        Setting a price on a market order implicitly converts it to a LIMIT
        order — a market order has no price, so a reviewer-supplied price is
        only meaningful as a limit.
        """
        request.overrides[key] = raw
        if key == "price":
            order_type = (request.arguments.get("order_type") or "").strip("'\"")
            if order_type.upper() == "MARKET":
                request.overrides["order_type"] = "LIMIT"
                return " (MARKET → LIMIT)"
        return ""

    async def _refresh_pending_message(self, pending: _PendingApproval) -> None:
        try:
            await self._application.bot.edit_message_text(
                chat_id=pending.chat_id,
                message_id=pending.message_id,
                text=self._build_pending_text(pending.request),
                reply_markup=self._build_keyboard(pending.request.id),
                parse_mode="HTML",
            )
        except TelegramError:
            logger.warning(
                "Failed to refresh Telegram approval message for request %s",
                pending.request.id,
            )

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
                parse_mode="HTML",
            )
        except TelegramError:
            logger.warning(
                "Failed to update Telegram approval message for request %s",
                pending.request.id,
            )

    @staticmethod
    def _editable_keys(request: ApprovalRequest) -> list[str]:
        return [key for key in ("quantity", "price") if key in request.arguments]

    @staticmethod
    def _build_pending_text(request: ApprovalRequest) -> str:
        lines = [
            "⚠️ Write operation requires approval",
            f"🔧 Tool: {html.escape(request.tool_name)}",
            f"🆔 Request ID: {html.escape(request.request_id)}",
        ]
        if request.client_id:
            lines.append(f"💻 Client ID: {html.escape(request.client_id)}")
        if request.arguments:
            lines.append("📋 Arguments:")
            lines.append(
                TelegramApprovalManager._format_arguments(
                    request.arguments, request.overrides
                )
            )
        editable = TelegramApprovalManager._editable_keys(request)
        if editable:
            lines.append(
                f"✏️ Reply to this message to adjust {'/'.join(editable)},"
                ' e.g. "qty 5 price 1.95".'
            )
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
            f"🔧 Tool: {html.escape(request.tool_name)}",
            f"🆔 Request ID: {html.escape(request.request_id)}",
        ]
        if request.client_id:
            lines.append(f"💻 Client ID: {html.escape(request.client_id)}")
        if request.arguments:
            lines.append("📋 Arguments:")
            lines.append(
                TelegramApprovalManager._format_arguments(
                    request.arguments, request.overrides
                )
            )
        if actor is not None:
            lines.append(
                f"👤 Actor: {html.escape(actor.full_name)} (ID: {actor.id})"
            )
        if reason:
            lines.append(f"📝 Notes: {html.escape(reason)}")
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
    def _format_arguments(
        arguments: Mapping[str, str],
        overrides: Mapping[str, str] | None = None,
    ) -> str:
        """Render arguments as an aligned monospace table (HTML <pre> block)."""
        overrides = overrides or {}
        visible = {
            key: value
            for key, value in arguments.items()
            if key not in TelegramApprovalManager._HIDDEN_ARG_KEYS
        }
        if not visible:
            return "(none)"

        key_width = max(len(key) for key in visible)
        rows = []
        for key, value in visible.items():
            emoji = TelegramApprovalManager._emoji_for_argument(key, value)
            prefix = f"{emoji} " if emoji else ""
            if len(value) >= 2 and value[0] == value[-1] == "'":
                value = value[1:-1]
            if key in overrides:
                value = f"{value} → {overrides[key]}"
            rows.append(f"{key.ljust(key_width)} │ {prefix}{value}")
        rendered = "\n".join(rows)
        if len(rendered) > 1000:
            rendered = f"{rendered[:997]}..."
        return f"<pre>{html.escape(rendered)}</pre>"

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
