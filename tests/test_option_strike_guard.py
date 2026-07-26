"""Guard against mis-built OCC strike fields.

The model builds the OCC symbol itself, and the strike field is
(strike price x 1000) zero-padded to 8 digits. A magnitude slip there still
produces a syntactically valid symbol for a *different* contract — $16.5
encoded as 00165000 ($165) instead of 00016500 — which the expiry guard
cannot catch. Observed live: three identical alerts produced 00016500 twice
and 00165000 once.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from schwab_mcp.tools.orders import (
    _assert_option_contract_exists,
    _validate_option_order_preapproval,
)

REAL = "SOFI  260731C00016500"  # $16.5
INVENTED = "SOFI  260731C00165000"  # $165 — same alert, mis-scaled strike


def _context(payload: dict[str, Any] | None = None, *, boom: Exception | None = None) -> Any:
    """Minimal stand-in for SchwabContext exposing just .client.get_quotes."""

    async def get_quotes(symbols: list[str]) -> Any:
        if boom is not None:
            raise boom
        return SimpleNamespace(json=lambda: payload)

    return SimpleNamespace(client=SimpleNamespace(get_quotes=get_quotes))


@pytest.mark.anyio
async def test_existing_contract_passes() -> None:
    ctx = _context({REAL: {"quote": {"mark": 0.85}}})
    await _assert_option_contract_exists(REAL, ctx)


@pytest.mark.anyio
async def test_invented_contract_rejected() -> None:
    # Schwab answers 200 but buckets unknown symbols under "errors"
    ctx = _context({"errors": {"invalidSymbols": [INVENTED]}})
    with pytest.raises(ValueError, match="does not exist at Schwab"):
        await _assert_option_contract_exists(INVENTED, ctx)


@pytest.mark.anyio
async def test_error_message_names_the_strike_encoding() -> None:
    ctx = _context({"errors": {"invalidSymbols": [INVENTED]}})
    with pytest.raises(ValueError) as excinfo:
        await _assert_option_contract_exists(INVENTED, ctx)
    # The model has to be able to self-correct from this text alone.
    assert "00016500" in str(excinfo.value)
    assert "00165000" in str(excinfo.value)


@pytest.mark.anyio
async def test_symbol_present_but_without_quote_is_rejected() -> None:
    ctx = _context({INVENTED: {}})
    with pytest.raises(ValueError, match="does not exist at Schwab"):
        await _assert_option_contract_exists(INVENTED, ctx)


@pytest.mark.anyio
async def test_transport_failure_fails_open() -> None:
    # A network blip must not block trading; Schwab's own preview still gates.
    ctx = _context(boom=RuntimeError("connection reset"))
    await _assert_option_contract_exists(REAL, ctx)


@pytest.mark.anyio
async def test_preapproval_runs_both_sync_and_existence_checks() -> None:
    ctx = _context({REAL: {"quote": {"mark": 0.85}}})
    await _validate_option_order_preapproval(
        {"symbol": REAL, "instruction": "BUY_TO_OPEN", "price": 0.87, "stop_price": 0.6},
        ctx,
    )


@pytest.mark.anyio
async def test_preapproval_still_enforces_stop_below_entry() -> None:
    ctx = _context({REAL: {"quote": {"mark": 0.85}}})
    with pytest.raises(ValueError, match="must be below the entry limit"):
        await _validate_option_order_preapproval(
            {"symbol": REAL, "instruction": "BUY_TO_OPEN", "price": 0.87, "stop_price": 1.5},
            ctx,
        )


@pytest.mark.anyio
async def test_preapproval_rejects_invented_symbol() -> None:
    ctx = _context({"errors": {"invalidSymbols": [INVENTED]}})
    with pytest.raises(ValueError, match="does not exist at Schwab"):
        await _validate_option_order_preapproval(
            {"symbol": INVENTED, "instruction": "BUY_TO_OPEN", "price": 0.87, "stop_price": 0.6},
            ctx,
        )
