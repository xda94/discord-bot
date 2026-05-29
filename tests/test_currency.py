"""Tests for currency utilities in `features.scraping`.

Covers the pure helpers (`_effective_currency`, `_majority_currency`) and
the `CurrencyConverter` class. The converter touches the DB (via
`get_exchange_rate`), so its tests use the `tmp_db` fixture and seed a
small rate table before running.
"""

import pytest

import db
from features.scraping import (
    CurrencyConverter,
    _effective_currency,
    _majority_currency,
)


@pytest.fixture
def seeded_rates(tmp_db):
    """Seed a minimal EUR-pivoted rate table. Each value is "units of
    <currency> per 1 EUR" — matching the format the live API returns.
    Numbers are illustrative, not market-accurate — tests only assert
    relative correctness."""
    db.set_exchange_rate("EUR", 1.0)        # pivot
    db.set_exchange_rate("DKK", 7.45)       # 1 EUR = 7.45 DKK
    db.set_exchange_rate("USD", 1.10)       # 1 EUR = 1.10 USD
    db.set_exchange_rate("RON", 4.95)       # 1 EUR = 4.95 RON
    return tmp_db


# ---------------------------------------------------------------------------
# _effective_currency
# ---------------------------------------------------------------------------

def test_effective_currency_prefers_stored():
    assert _effective_currency("EUR", "https://example.ro/x") == "EUR"


def test_effective_currency_falls_back_to_tld_ro():
    assert _effective_currency(None, "https://example.ro/x") == "RON"


def test_effective_currency_falls_back_to_tld_dk():
    assert _effective_currency(None, "https://example.dk/x") == "DKK"


def test_effective_currency_handles_subdomain():
    assert _effective_currency(None, "https://shop.altex.ro/x") == "RON"


def test_effective_currency_unknown_tld_returns_none():
    assert _effective_currency(None, "https://example.com/x") is None


def test_effective_currency_empty_string_treated_as_missing():
    """`""` is falsy so `stored or fallback` picks the fallback. We want
    the TLD guess in that case, not the empty string back."""
    assert _effective_currency("", "https://example.ro/x") == "RON"


# ---------------------------------------------------------------------------
# _majority_currency
# ---------------------------------------------------------------------------

def test_majority_currency_picks_most_common():
    pairs = [
        ("https://a.ro/", None),
        ("https://b.ro/", None),
        ("https://c.dk/", None),
    ]
    assert _majority_currency(pairs) == "RON"


def test_majority_currency_default_when_no_known():
    pairs = [("https://a.com/", None), ("https://b.com/", None)]
    assert _majority_currency(pairs) == CurrencyConverter.DEFAULT_DISPLAY_CURRENCY


def test_majority_currency_default_when_empty():
    assert _majority_currency([]) == CurrencyConverter.DEFAULT_DISPLAY_CURRENCY


def test_majority_currency_ties_broken_by_insertion_order():
    """Counter.most_common preserves insertion order on ties — RON inserted
    first, so RON wins the 1-vs-1 tie."""
    pairs = [
        ("https://a.ro/", None),
        ("https://b.dk/", None),
    ]
    assert _majority_currency(pairs) == "RON"


def test_majority_currency_normalises_case():
    pairs = [("https://a.com/", "eur"), ("https://b.com/", "EUR")]
    assert _majority_currency(pairs) == "EUR"


def test_majority_currency_uses_stored_over_tld():
    """If `stored` is given, it should win over the TLD guess even when the
    TLD would suggest something different."""
    pairs = [("https://a.ro/", "USD"), ("https://b.ro/", "USD")]
    assert _majority_currency(pairs) == "USD"


# ---------------------------------------------------------------------------
# CurrencyConverter.convert
# ---------------------------------------------------------------------------

def test_convert_same_currency_returns_input(seeded_rates):
    cv = CurrencyConverter()
    assert cv.convert(100, "EUR", "EUR") == pytest.approx(100)


def test_convert_via_eur_pivot(seeded_rates):
    cv = CurrencyConverter()
    # Stored rates are "units per 1 EUR", so 100 EUR → RON is just the
    # multiplication by the RON rate: 100 × 4.95 = 495 RON.
    assert cv.convert(100, "EUR", "RON") == pytest.approx(495.0, rel=1e-6)


def test_convert_cross_currency_lifts_through_eur(seeded_rates):
    """Non-EUR source: lift to EUR (divide by source rate), then drop
    into target (multiply by target rate). 100 DKK → 100/7.45 EUR ≈
    13.42 EUR → ×1.10 USD ≈ 14.77 USD."""
    cv = CurrencyConverter()
    expected = (100 / 7.45) * 1.10
    assert cv.convert(100, "DKK", "USD") == pytest.approx(expected, rel=1e-6)


def test_convert_missing_source_rate_returns_none(seeded_rates):
    cv = CurrencyConverter()
    assert cv.convert(100, "XYZ", "EUR") is None


def test_convert_missing_target_rate_returns_none(seeded_rates):
    cv = CurrencyConverter()
    assert cv.convert(100, "EUR", "XYZ") is None


def test_convert_none_price_returns_none(seeded_rates):
    cv = CurrencyConverter()
    assert cv.convert(None, "EUR", "DKK") is None


def test_convert_non_numeric_price_returns_none(seeded_rates):
    cv = CurrencyConverter()
    assert cv.convert("not a number", "EUR", "DKK") is None


# ---------------------------------------------------------------------------
# CurrencyConverter.to_currency (best-effort wrapper)
# ---------------------------------------------------------------------------

def test_to_currency_same_currency(seeded_rates):
    cv = CurrencyConverter()
    assert cv.to_currency(100, "EUR", "EUR") == 100.0


def test_to_currency_unknown_source_returns_none(seeded_rates):
    cv = CurrencyConverter()
    assert cv.to_currency(100, None, "EUR") is None


def test_to_currency_case_insensitive(seeded_rates):
    cv = CurrencyConverter()
    assert cv.to_currency(100, "eur", "EUR") == 100.0


# ---------------------------------------------------------------------------
# CurrencyConverter.format_in_currency
# ---------------------------------------------------------------------------

def test_format_unknown_source_marked_question_mark(seeded_rates):
    cv = CurrencyConverter()
    assert cv.format_in_currency(100, None, "EUR") == "100.00 (?)"


def test_format_same_unit_no_conversion_suffix(seeded_rates):
    cv = CurrencyConverter()
    assert cv.format_in_currency(100, "EUR", "EUR") == "100.00 EUR"


def test_format_successful_conversion(seeded_rates):
    cv = CurrencyConverter()
    # 100 DKK → DKK is trivially 100. Non-trivial path:
    val = cv.format_in_currency(100, "EUR", "DKK")
    assert val == "745.00 DKK"


def test_format_missing_rate_marked_asterisk(seeded_rates):
    """Source currency is known but no rate exists — show raw price in the
    source unit with an asterisk so the user can see it wasn't converted."""
    cv = CurrencyConverter()
    assert cv.format_in_currency(100, "XYZ", "EUR") == "100.00 XYZ*"


def test_format_none_price_returns_na(seeded_rates):
    cv = CurrencyConverter()
    assert cv.format_in_currency(None, "EUR", "DKK") == "N/A"


def test_format_non_numeric_price_returns_na(seeded_rates):
    cv = CurrencyConverter()
    assert cv.format_in_currency("bad", "EUR", "DKK") == "N/A"
