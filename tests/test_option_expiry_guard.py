from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

import pytest

from schwab_mcp.tools.orders import _validate_option_expiry


def _occ(expiry: datetime.date, root: str = "MSFT") -> str:
    return f"{root:<6}{expiry:%y%m%d}C00407500"


def _today_eastern() -> datetime.date:
    return datetime.datetime.now(ZoneInfo("America/New_York")).date()


def test_future_expiry_passes() -> None:
    _validate_option_expiry(_occ(_today_eastern() + datetime.timedelta(days=7)))


def test_same_day_expiry_passes() -> None:
    # 0DTE is a legitimate trade
    _validate_option_expiry(_occ(_today_eastern()))


def test_past_expiry_rejected() -> None:
    with pytest.raises(ValueError, match="in the past"):
        _validate_option_expiry(_occ(_today_eastern() - datetime.timedelta(days=1)))


def test_wrong_year_digits_rejected() -> None:
    # The observed failure class: model wrote year 25 instead of 26
    last_year = _today_eastern() - datetime.timedelta(days=365)
    with pytest.raises(ValueError, match="in the past"):
        _validate_option_expiry(_occ(last_year))


def test_extra_padding_tolerated() -> None:
    # Observed: a mis-padded root ("IBM    " with 4 spaces) — date check
    # still applies rather than failing to parse
    future = _today_eastern() + datetime.timedelta(days=7)
    _validate_option_expiry(f"IBM    {future:%y%m%d}C00220000")


def test_garbage_symbol_rejected() -> None:
    with pytest.raises(ValueError, match="Not a parsable OCC"):
        _validate_option_expiry("NOT-AN-OPTION")


def test_invalid_calendar_date_rejected() -> None:
    with pytest.raises(ValueError, match="invalid expiry date"):
        _validate_option_expiry("MSFT  261332C00407500")
