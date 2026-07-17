from __future__ import annotations

import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from schwab_mcp.tools import orders
from conftest import DummyPlaceOrderClient, make_ctx, run


def _future_occ() -> str:
    future = datetime.datetime.now(ZoneInfo("America/New_York")).date() + datetime.timedelta(days=7)
    return f"MSFT  {future:%y%m%d}C00407500"


REJECT_PAYLOAD = {
    "orderValidationResult": {
        "rejects": [{"message": "Security is not tradable"}],
    }
}

WARN_ONLY_PAYLOAD = {
    "orderValidationResult": {
        "rejects": [],
        "warns": [{"message": "Market is closed; order queued"}],
    }
}


def _client(order_response_factory: Any, preview_payload: Any = None) -> DummyPlaceOrderClient:
    response = order_response_factory(account_hash="hash", order_id=42)
    return DummyPlaceOrderClient(order_response=response, preview_payload=preview_payload)


def test_clean_preview_places_order(order_response_factory) -> None:
    client = _client(order_response_factory)
    ctx = make_ctx(client)
    result = run(
        orders.place_option_order(
            ctx, "hash", _future_occ(), 5, "BUY_TO_OPEN", "LIMIT", price=1.81
        )
    )
    assert result["orderId"] == 42
    assert client.preview_captured is not None  # preview ran
    assert client.captured is not None  # order placed


def test_preview_reject_blocks_placement(order_response_factory) -> None:
    client = _client(order_response_factory, preview_payload=REJECT_PAYLOAD)
    ctx = make_ctx(client)
    with pytest.raises(ValueError, match="Security is not tradable"):
        run(
            orders.place_option_order(
                ctx, "hash", _future_occ(), 5, "BUY_TO_OPEN", "LIMIT", price=1.81
            )
        )
    assert client.captured is None  # place_order never called


def test_warnings_do_not_block(order_response_factory) -> None:
    client = _client(order_response_factory, preview_payload=WARN_ONLY_PAYLOAD)
    ctx = make_ctx(client)
    result = run(
        orders.place_option_order(
            ctx, "hash", _future_occ(), 5, "BUY_TO_OPEN", "LIMIT", price=1.81
        )
    )
    assert result["orderId"] == 42


def test_preview_failure_fails_open(order_response_factory) -> None:
    client = _client(order_response_factory)

    async def broken_preview(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("preview endpoint down")

    client.preview_order = broken_preview  # type: ignore[method-assign]
    ctx = make_ctx(client)
    result = run(
        orders.place_option_order(
            ctx, "hash", _future_occ(), 5, "BUY_TO_OPEN", "LIMIT", price=1.81
        )
    )
    assert result["orderId"] == 42  # placed despite broken preview


def test_client_without_preview_still_places(order_response_factory) -> None:
    client = _client(order_response_factory)
    client.preview_order = None  # type: ignore[assignment]
    ctx = make_ctx(client)
    result = run(
        orders.place_option_order(
            ctx, "hash", _future_occ(), 5, "BUY_TO_OPEN", "LIMIT", price=1.81
        )
    )
    assert result["orderId"] == 42


def test_entry_with_stop_previews_trigger_order(order_response_factory) -> None:
    client = _client(order_response_factory)
    ctx = make_ctx(client)
    result = run(
        orders.place_option_entry_with_stop(
            ctx, "hash", _future_occ(), 8, price=1.81, stop_price=1.5
        )
    )
    assert result["orderId"] == 42
    previewed = client.preview_captured["kwargs"]["order_spec"]
    placed = client.captured["kwargs"]["order_spec"]
    assert previewed == placed  # gate previews exactly what gets placed
    assert placed["orderStrategyType"] == "TRIGGER"


def test_entry_with_stop_preview_reject_blocks(order_response_factory) -> None:
    client = _client(order_response_factory, preview_payload=REJECT_PAYLOAD)
    ctx = make_ctx(client)
    with pytest.raises(ValueError, match="preview rejected"):
        run(
            orders.place_option_entry_with_stop(
                ctx, "hash", _future_occ(), 8, price=1.81, stop_price=1.5
            )
        )
    assert client.captured is None


def test_plain_order_with_stop_price_builds_trigger(order_response_factory) -> None:
    client = _client(order_response_factory)
    ctx = make_ctx(client)
    result = run(
        orders.place_option_order(
            ctx, "hash", _future_occ(), 5, "BUY_TO_OPEN", "LIMIT",
            price=1.81, stop_price=1.5,
        )
    )
    assert result["orderId"] == 42
    placed = client.captured["kwargs"]["order_spec"]
    assert placed["orderStrategyType"] == "TRIGGER"
    child = placed["childOrderStrategies"][0]
    assert child["orderType"] == "STOP"
    assert child["duration"] == "GOOD_TILL_CANCEL"


def test_stop_price_rejected_for_sells(order_response_factory) -> None:
    client = _client(order_response_factory)
    ctx = make_ctx(client)
    with pytest.raises(ValueError, match="BUY_TO_OPEN"):
        run(
            orders.place_option_order(
                ctx, "hash", _future_occ(), 5, "SELL_TO_CLOSE", "LIMIT",
                price=2.0, stop_price=1.5,
            )
        )
    assert client.captured is None


def test_stop_price_rejected_for_market(order_response_factory) -> None:
    client = _client(order_response_factory)
    ctx = make_ctx(client)
    with pytest.raises(ValueError, match="LIMIT order"):
        run(
            orders.place_option_order(
                ctx, "hash", _future_occ(), 5, "BUY_TO_OPEN", "MARKET",
                stop_price=1.5,
            )
        )
    assert client.captured is None


def test_stop_price_must_be_below_entry(order_response_factory) -> None:
    client = _client(order_response_factory)
    ctx = make_ctx(client)
    with pytest.raises(ValueError, match="below the entry"):
        run(
            orders.place_option_order(
                ctx, "hash", _future_occ(), 5, "BUY_TO_OPEN", "LIMIT",
                price=1.81, stop_price=2.5,
            )
        )
    assert client.captured is None
