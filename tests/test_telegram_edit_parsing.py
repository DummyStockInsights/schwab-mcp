from __future__ import annotations

from schwab_mcp.approvals import ApprovalRequest
from schwab_mcp.approvals.telegram import TelegramApprovalManager


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


def test_format_arguments_shows_overrides() -> None:
    request = make_request({"quantity": "10", "price": "2.01"})
    rendered = TelegramApprovalManager._format_arguments(
        request.arguments, {"quantity": "5"}
    )
    assert "10 → 5" in rendered
    assert "2.01" in rendered


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
