from __future__ import annotations

import asyncio

import pytest

from schwab_mcp.approvals.telegram import (
    TelegramApprovalManager,
    TelegramApprovalSettings,
)


class _FakeUpdater:
    """Minimal stand-in for PTB's Updater.

    running flips to False to simulate a silently-died poll loop; each
    start_polling() call bumps a counter and marks it running again.
    """

    def __init__(self) -> None:
        self.running = True
        self.start_calls = 0

    async def start_polling(self) -> None:
        self.start_calls += 1
        self.running = True


class _FakeApplication:
    def __init__(self, updater: _FakeUpdater) -> None:
        self.updater = updater


def _manager() -> TelegramApprovalManager:
    settings = TelegramApprovalSettings(
        token="x", chat_id=1, approver_ids=frozenset({1})
    )
    mgr = TelegramApprovalManager(settings)
    mgr._watchdog_interval = 0.01  # fast loop for the test
    return mgr


def test_watchdog_restarts_dead_polling() -> None:
    async def scenario() -> _FakeUpdater:
        mgr = _manager()
        updater = _FakeUpdater()
        mgr._application = _FakeApplication(updater)  # type: ignore[assignment]
        mgr._started = True
        task = asyncio.ensure_future(mgr._watchdog_loop())

        # Simulate the poll loop dying.
        updater.running = False
        # Give the watchdog a few iterations to notice and recover.
        await asyncio.sleep(0.1)

        mgr._started = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return updater

    updater = asyncio.run(scenario())
    assert updater.start_calls >= 1  # watchdog restarted polling
    assert updater.running is True


def test_watchdog_leaves_healthy_polling_alone() -> None:
    async def scenario() -> _FakeUpdater:
        mgr = _manager()
        updater = _FakeUpdater()  # stays running
        mgr._application = _FakeApplication(updater)  # type: ignore[assignment]
        mgr._started = True
        task = asyncio.ensure_future(mgr._watchdog_loop())
        await asyncio.sleep(0.1)
        mgr._started = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return updater

    updater = asyncio.run(scenario())
    assert updater.start_calls == 0  # never touched a healthy updater
