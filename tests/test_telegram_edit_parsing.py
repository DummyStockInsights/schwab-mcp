from __future__ import annotations

import inspect

import pytest

from schwab_mcp.approvals import ApprovalRequest
from schwab_mcp.approvals.telegram import TelegramApprovalManager
from schwab_mcp.tools import orders


def stringified_arguments(func, /, **values) -> dict[str, str]:
    """Mirror how _wrap_with_approval stringifies a tool call's arguments."""
    signature = inspect.signature(func)
    bound = signature.bind_partial(ctx=None, **values)
    bound.apply_defaults()
    return {
        name: repr(value)
        for name, value in bound.arguments.items()
        if name != "ctx"
    }


def make_request(arguments: dict[str, str]) -> ApprovalRequest:
    return ApprovalRequest(
        id="req",
        tool_name="place_option_order",
        request_id="1",
        client_id=None,
        arguments=arguments,
    )


def test_parse_edit_text_variants() -> None:
    parse = TelegramApprovalManager._parse_edit_text
    assert parse("qty 5") == {"quantity": "5"}
    assert parse("quantity=5") == {"quantity": "5"}
    assert parse("price: 1.95") == {"price": "1.95"}
    assert parse("qty 5 price 1.95") == {"quantity": "5", "price": "1.95"}
    assert parse("数量 5 价格 1.95") == {"quantity": "5", "price": "1.95"}
    assert parse("stop 1.4") == {"stop_price": "1.4"}
    assert parse("stop_price 1.4") == {"stop_price": "1.4"}
    assert parse("止损 1.4") == {"stop_price": "1.4"}
    assert parse("qty 5 stop 1.4") == {"quantity": "5", "stop_price": "1.4"}
    assert parse("hello world") == {}


def test_validate_override_quantity() -> None:
    request = make_request({"quantity": "10", "price": "2.01"})
    validate = TelegramApprovalManager._validate_override

    assert validate(request, "quantity", "5") == (True, "")
    ok, why = validate(request, "quantity", "5.5")
    assert not ok and "whole number" in why
    ok, why = validate(request, "quantity", "0")
    assert not ok and "positive" in why


def test_validate_override_price() -> None:
    validate = TelegramApprovalManager._validate_override

    request = make_request({"quantity": "10", "price": "2.01"})
    assert validate(request, "price", "1.95") == (True, "")

    market = make_request({"quantity": "10", "price": "None"})
    assert validate(market, "price", "1.95") == (True, "")

    missing = make_request({"quantity": "10"})
    ok, why = validate(missing, "price", "1.95")
    assert not ok and "not part of this order" in why


def test_record_override_converts_market_to_limit() -> None:
    record = TelegramApprovalManager._record_override

    market = make_request(
        {"quantity": "10", "price": "None", "order_type": "'MARKET'"}
    )
    note = record(market, "price", "1.95")
    assert market.overrides == {"price": "1.95", "order_type": "LIMIT"}
    assert "MARKET → LIMIT" in note

    limit = make_request(
        {"quantity": "10", "price": "2.01", "order_type": "'LIMIT'"}
    )
    note = record(limit, "price", "1.95")
    assert limit.overrides == {"price": "1.95"}
    assert note == ""

    qty_only = make_request(
        {"quantity": "10", "price": "None", "order_type": "'MARKET'"}
    )
    note = record(qty_only, "quantity", "5")
    assert qty_only.overrides == {"quantity": "5"}
    assert note == ""


def test_validate_override_stop_price() -> None:
    validate = TelegramApprovalManager._validate_override

    bracket = make_request(
        {"quantity": "8", "price": "1.81", "stop_price": "1.5"}
    )
    assert validate(bracket, "stop_price", "1.4") == (True, "")
    ok, why = validate(bracket, "stop_price", "2.5")
    assert not ok and "below the entry price" in why
    ok, why = validate(bracket, "price", "1.4")
    assert not ok and "above the stop_price" in why

    plain = make_request({"quantity": "10", "price": "2.01"})
    ok, why = validate(plain, "stop_price", "1.4")
    assert not ok and "not part of this order" in why


def test_format_arguments_shows_overrides() -> None:
    request = make_request({"quantity": "10", "price": "2.01"})
    rendered = TelegramApprovalManager._format_arguments(
        request.arguments, {"quantity": "5"}
    )
    assert "10 → 5" in rendered
    assert "2.01" in rendered


@pytest.mark.parametrize(
    "func,call_kwargs",
    [
        (
            orders.place_option_order,
            dict(
                account_hash="hash",
                symbol="MSFT  260710C00410000",
                quantity=10,
                instruction="BUY_TO_OPEN",
                order_type="MARKET",
            ),
        ),
        (
            orders.place_option_order,
            dict(
                account_hash="hash",
                symbol="MSFT  260710C00410000",
                quantity=5,
                instruction="SELL_TO_CLOSE",
                order_type="LIMIT",
                price=2.49,
            ),
        ),
        (
            orders.place_option_entry_with_stop,
            dict(
                account_hash="hash",
                symbol="MSFT  260717C00407500",
                quantity=8,
                price=1.81,
                stop_price=1.5,
            ),
        ),
    ],
)
def test_every_pilot_order_is_qty_and_price_editable(func, call_kwargs) -> None:
    """Hard guarantee: any order the pilot tools can place accepts
    quantity AND price edits during approval, regardless of order type
    or direction."""
    request = make_request(stringified_arguments(func, **call_kwargs))

    expected = ["quantity", "price"]
    if "stop_price" in call_kwargs:
        expected.append("stop_price")
    assert TelegramApprovalManager._editable_keys(request) == expected
    validate = TelegramApprovalManager._validate_override
    assert validate(request, "quantity", "3") == (True, "")
    # 9.99 stays above any stop_price in the fixtures, so the cross-field
    # stop<price guard doesn't reject it
    assert validate(request, "price", "9.99") == (True, "")
    assert "Reply to this message to adjust quantity/price" in (
        TelegramApprovalManager._build_pending_text(request)
    )


def test_pending_text_includes_edit_hint() -> None:
    request = make_request({"quantity": "10", "price": "2.01"})
    text = TelegramApprovalManager._build_pending_text(request)
    assert "Reply to this message to adjust quantity/price" in text

    market = make_request({"quantity": "10", "price": "None"})
    text = TelegramApprovalManager._build_pending_text(market)
    assert "Reply to this message to adjust quantity/price" in text

    no_args = make_request({"symbol": "'F'"})
    text = TelegramApprovalManager._build_pending_text(no_args)
    assert "Reply to this message" not in text
