"""Mastercopy parser tests — value-level helpers + skip rules."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from src.wip_app.imports.mastercopy_parser import (
    ParseError,
    parse_case_reference,
    parse_date,
    parse_decimal,
)


# ---------------------------------------------------------------------------
# parse_decimal — DECIMAL safety per architect rule
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (None, None),
    ("", None),
    ("-", None),
    ("N/A", None),
    (1234, Decimal("1234")),
    (1234.56, Decimal("1234.56")),
    ("£1,234.56", Decimal("1234.56")),
    ("  1234.56  ", Decimal("1234.56")),
    (Decimal("100"), Decimal("100")),
])
def test_parse_decimal_clean(raw, expected):
    assert parse_decimal(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "12abc", "1.2.3", "£not-a-number"])
def test_parse_decimal_rejects_garbage(raw):
    with pytest.raises(ParseError):
        parse_decimal(raw)


def test_parse_decimal_preserves_precision_from_float():
    """Floats from Excel must not introduce binary-float noise — round-trip
    through str so '0.1' really is 0.10 not 0.10000000000000001.
    """
    d = parse_decimal(0.1)
    assert str(d) == "0.1"


# ---------------------------------------------------------------------------
# parse_date
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (None, None),
    ("", None),
    (date(2026, 5, 28), date(2026, 5, 28)),
    ("2026-05-28", date(2026, 5, 28)),
    ("28/05/2026", date(2026, 5, 28)),
    ("28-05-2026", date(2026, 5, 28)),
])
def test_parse_date_clean(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", ["nope", "32/13/2026", "yesterday"])
def test_parse_date_rejects_garbage(raw):
    with pytest.raises(ParseError):
        parse_date(raw)


# ---------------------------------------------------------------------------
# parse_case_reference — strips floats, returns None on non-numeric
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (None, None),
    ("", None),
    (12345, "12345"),
    (12345.0, "12345"),
    ("12345", "12345"),
    ("  12345  ", "12345"),
    ("Totals", None),         # non-numeric (summary row footer)
    ("ABC-123", None),        # non-numeric — caller will reject as missing_case_ref
    ("12345.0", "12345"),
])
def test_parse_case_reference(raw, expected):
    assert parse_case_reference(raw) == expected
