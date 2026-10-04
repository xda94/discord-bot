"""Tests for the pure scraping helpers in `features.wishlist`.

These all run without a network round-trip — they exercise the URL
validator, the TLD currency fallback, and the JSON-LD / meta / text-fallback
extractors against canned HTML fixtures.

"""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from bs4 import BeautifulSoup

from features.wishlist import PriceScraper, _is_valid_http_url
from llm.capacity import CapacityBusy, CapacityError, CapacityTimeout
from wishlist.scraper import FAILURE_BUSY, FAILURE_UNSUPPORTED, ScrapeResult


def _parse(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


# ---------------------------------------------------------------------------
# _is_valid_http_url
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://example.com",
    "http://example.com/path",
    "https://shop.altex.ro/product/123",
    "  https://example.com  ",  # leading/trailing whitespace stripped
])
def test_is_valid_http_url_accepts(url):
    assert _is_valid_http_url(url) is True


@pytest.mark.parametrize("url", [
    "",
    "   ",
    None,
    "javascript:alert(1)",
    "file:///etc/passwd",
    "data:text/html,<h1>x</h1>",
    "ftp://example.com",
    "http://",
    "not a url at all",
    123,  # non-string
])
def test_is_valid_http_url_rejects(url):
    assert _is_valid_http_url(url) is False


# ---------------------------------------------------------------------------
# PriceScraper._currency_from_tld
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://altex.ro/product", "RON"),
    ("https://www.altex.ro/product", "RON"),
    ("https://shop.altex.ro/product", "RON"),
    ("https://example.dk/x", "DKK"),
    ("https://www.example.dk", "DKK"),
])
def test_currency_from_tld_known(url, expected):
    assert PriceScraper._currency_from_tld(url) == expected


@pytest.mark.parametrize("url", [
    "https://example.com/x",
    "https://example.org",
    "https://example.co.uk",
    "not a url",
    "",
])
def test_currency_from_tld_unknown(url):
    assert PriceScraper._currency_from_tld(url) is None


# ---------------------------------------------------------------------------
# _extract_from_json_ld
# ---------------------------------------------------------------------------

def test_json_ld_basic_product():
    soup = _parse('''
        <script type="application/ld+json">
        {
          "@type": "Product",
          "name": "Test Item",
          "offers": {
            "@type": "Offer",
            "price": "99.99",
            "priceCurrency": "EUR",
            "availability": "https://schema.org/InStock"
          }
        }
        </script>
    ''')
    price, title, currency, in_stock = PriceScraper._extract_from_json_ld(
        soup, None, None, None, None,
    )
    assert price == 99.99
    assert title == "Test Item"
    assert currency == "EUR"
    assert in_stock is True


def test_json_ld_nested_price_specification():
    """Altex/eMag put the price inside a nested `priceSpecification` rather
    than directly on the offer — the extractor must look there too."""
    soup = _parse('''
        <script type="application/ld+json">
        {
          "@type": "Product",
          "name": "Nested",
          "offers": {
            "priceSpecification": {
              "price": "1234.50",
              "priceCurrency": "RON"
            },
            "availability": "OutOfStock"
          }
        }
        </script>
    ''')
    price, title, currency, in_stock = PriceScraper._extract_from_json_ld(
        soup, None, None, None, None,
    )
    assert price == 1234.50
    assert currency == "RON"
    assert in_stock is False


def test_json_ld_comma_decimal_separator():
    """European number format with a comma as the decimal separator."""
    soup = _parse('''
        <script type="application/ld+json">
        {"@type": "Product", "offers": {"price": "1234,56", "priceCurrency": "EUR"}}
        </script>
    ''')
    price, *_ = PriceScraper._extract_from_json_ld(soup, None, None, None, None)
    assert price == 1234.56


def test_json_ld_offers_as_list():
    """`offers` can be a JSON array; first offer should win."""
    soup = _parse('''
        <script type="application/ld+json">
        {
          "@type": "Product",
          "offers": [
            {"price": "10.00", "priceCurrency": "USD"},
            {"price": "20.00", "priceCurrency": "USD"}
          ]
        }
        </script>
    ''')
    price, *_ = PriceScraper._extract_from_json_ld(soup, None, None, None, None)
    assert price == 10.00


def test_json_ld_malformed_block_skipped():
    """A broken JSON-LD block must not crash the whole parse — the
    extractor should continue to the next block."""
    soup = _parse('''
        <script type="application/ld+json">{not valid json</script>
        <script type="application/ld+json">
        {"@type": "Product", "name": "Saved"}
        </script>
    ''')
    _, title, *_ = PriceScraper._extract_from_json_ld(soup, None, None, None, None)
    assert title == "Saved"


def test_json_ld_no_product_block_returns_seeded_values():
    """When no Product schema is present, the extractor returns whatever
    seed values it was called with — i.e. it doesn't overwrite with None."""
    soup = _parse('<p>no structured data here</p>')
    price, title, currency, in_stock = PriceScraper._extract_from_json_ld(
        soup, 9.99, "seed-title", "EUR", True,
    )
    assert price == 9.99
    assert title == "seed-title"
    assert currency == "EUR"
    assert in_stock is True


def test_json_ld_preorder_treated_as_in_stock():
    """`PreOrder` availability counts as in-stock for our purposes."""
    soup = _parse('''
        <script type="application/ld+json">
        {"@type": "Product", "offers": {"availability": "https://schema.org/PreOrder"}}
        </script>
    ''')
    *_, in_stock = PriceScraper._extract_from_json_ld(soup, None, None, None, None)
    assert in_stock is True


# ---------------------------------------------------------------------------
# Meta-tag extractors
# ---------------------------------------------------------------------------

def test_meta_title_from_og_property():
    soup = _parse('<meta property="og:title" content="Hello">')
    assert PriceScraper._extract_meta_title(soup) == "Hello"


def test_meta_title_falls_back_to_title_tag():
    soup = _parse("<title>Page Title</title>")
    assert PriceScraper._extract_meta_title(soup) == "Page Title"


def test_meta_price_dot_decimal():
    soup = _parse('<meta property="product:price:amount" content="99.99">')
    assert PriceScraper._extract_meta_price(soup) == 99.99


def test_meta_price_comma_decimal():
    soup = _parse('<meta property="product:price:amount" content="1234,56">')
    assert PriceScraper._extract_meta_price(soup) == 1234.56


def test_meta_currency():
    soup = _parse('<meta property="product:price:currency" content="EUR">')
    assert PriceScraper._extract_meta_currency(soup) == "EUR"


def test_meta_currency_missing_returns_none():
    soup = _parse("<p>nothing</p>")
    assert PriceScraper._extract_meta_currency(soup) is None


# ---------------------------------------------------------------------------
# Text-fallback availability detection
# ---------------------------------------------------------------------------

def test_text_availability_negative_keyword_wins():
    """If both positive ("add to cart") and negative ("stoc epuizat")
    keywords appear on the page, the OOS signal must win — otherwise a
    generic "in stoc" elsewhere on the page would mask the real status."""
    html = "<p>add to cart</p><p>stoc epuizat</p>"
    assert PriceScraper._extract_text_availability(html) is False


def test_text_availability_positive_only():
    assert PriceScraper._extract_text_availability("<p>add to cart</p>") is True


def test_text_availability_romanian_in_stock():
    assert PriceScraper._extract_text_availability("<p>în stoc</p>") is True


def test_text_availability_no_signal_returns_none():
    """No keyword match in either direction → unknown (not False). This is
    what lets the downstream loop avoid mis-stamping items as OOS."""
    assert PriceScraper._extract_text_availability("<p>just a desc</p>") is None

def test_fetch_ignores_script_tags():
    """Verify that fetch strips script tags before falling back to text availability.
    This prevents matching JSON payloads containing out-of-stock keys."""
    class DummyResponse:
        status_code = 200
        text = '''
        <html>
            <body><p>in stoc</p></body>
            <script>const state = {"status": "stoc epuizat"};</script>
        </html>
        '''
    
    scraper = PriceScraper()
    scraper._http_get = lambda url: DummyResponse()
    
    result = scraper.fetch("https://example.com/test")
    # "stoc epuizat" is in the script, so if it's not stripped, it would win.
    # Since it is stripped, the "in stoc" in the body should win.
    assert result.in_stock is True


@pytest.mark.parametrize("price", [True, False, -1, "-1", float("nan"), float("inf"), "NaN", "Infinity", "", "bad"])
def test_scrape_result_rejects_invalid_price(price):
    result = ScrapeResult(price=price, title="Item", currency="EUR")
    assert result.price is None
    assert result.has_data is False


@pytest.mark.parametrize("result", [ScrapeResult(price=0), ScrapeResult(in_stock=False), ScrapeResult(in_stock=True)])
def test_scrape_result_usable_evidence(result):
    assert result.has_data is True


@pytest.mark.parametrize("result", [ScrapeResult(title="Item"), ScrapeResult(currency="RON"), ScrapeResult(in_stock=0)])
def test_scrape_result_metadata_is_not_usable_evidence(result):
    assert result.has_data is False


@pytest.mark.parametrize("price", [True, False, -1, "bad", "", "NaN", "Infinity"])
def test_json_ld_invalid_price_is_unknown(price):
    soup = _parse('<script type="application/ld+json">' + json.dumps({
        "@type": "Product", "offers": {"price": price}
    }) + '</script>')
    assert PriceScraper._extract_from_json_ld(soup, None, None, None, None)[0] is None


@pytest.mark.parametrize("price", ["-1", "bad", "", "NaN", "Infinity"])
def test_meta_invalid_price_is_unknown(price):
    soup = _parse(f'<meta property="product:price:amount" content="{price}">')
    assert PriceScraper._extract_meta_price(soup) is None


@pytest.mark.parametrize("value,expected", [
    ("https://schema.org/InStock", True), ("PreOrder", True),
    ("https://schema.org/OutOfStock", False), ("out of stock", False),
    ("SoldOut", False), ("", None), ("https://schema.org/BackOrder", None),
    ("unknown", None),
])
def test_structured_availability_preserves_unknown_and_false(value, expected):
    soup = _parse('<script type="application/ld+json">' + json.dumps({
        "@type": "Product", "offers": {"availability": value}
    }) + '</script>')
    assert PriceScraper._extract_from_json_ld(soup, None, None, None, None)[3] is expected
    soup = _parse(f'<meta property="product:availability" content="{value}">')
    assert PriceScraper._extract_meta_availability(soup) is expected


@pytest.mark.parametrize("html,url,currency", [
    ("<title>Site</title>", "https://example.com/item", None),
    ('<meta property="product:price:currency" content="EUR">', "https://example.com/item", "EUR"),
    ("<p></p>", "https://example.ro/item", "RON"),
    ("<p></p>", "https://example.dk/item", "DKK"),
])
def test_fetch_metadata_only_runs_fallback_and_stays_unsupported(monkeypatch, html, url, currency):
    scraper = PriceScraper()
    monkeypatch.setattr(scraper, "_http_get", lambda _url: SimpleNamespace(status_code=200, text=html))
    fallback = Mock(return_value=(None, "Fallback title", None, None))
    monkeypatch.setattr(scraper, "_extract_with_llm", fallback)
    result = scraper.fetch(url)
    fallback.assert_called_once()
    assert result.failure == FAILURE_UNSUPPORTED
    assert result.currency == currency
    assert result.has_data is False


def test_fetch_fallback_preserves_metadata_and_currency_precedes_tld(monkeypatch):
    scraper = PriceScraper()
    html = '<title>Source title</title>'
    monkeypatch.setattr(scraper, "_http_get", lambda _url: SimpleNamespace(status_code=200, text=html))
    monkeypatch.setattr(scraper, "_extract_with_llm", Mock(return_value=(0, "Fallback title", "EUR", False)))
    result = scraper.fetch("https://example.ro/item")
    assert result == ScrapeResult(price=0, title="Source title", currency="EUR", in_stock=False)


def test_fetch_fallback_keeps_explicit_currency(monkeypatch):
    scraper = PriceScraper()
    html = '<meta property="product:price:currency" content="RON">'
    monkeypatch.setattr(scraper, "_http_get", lambda _url: SimpleNamespace(status_code=200, text=html))
    monkeypatch.setattr(scraper, "_extract_with_llm", Mock(return_value=(10, "Title", "EUR", None)))
    result = scraper.fetch("https://example.ro/item")
    assert result.currency == "RON"
    assert result.price == 10


def test_fetch_zero_and_false_structured_data_take_precedence(monkeypatch):
    scraper = PriceScraper()
    html = '''<script type="application/ld+json">
    {"@type":"Product","name":"Structured","offers":[
      {"price":0,"priceCurrency":"EUR","availability":"OutOfStock"},
      {"price":9,"priceCurrency":"USD","availability":"InStock"}]}
    </script><meta property="product:price:amount" content="10">
    <meta property="product:availability" content="InStock"><p>add to cart</p>'''
    monkeypatch.setattr(scraper, "_http_get", lambda _url: SimpleNamespace(status_code=200, text=html))
    fallback = Mock(side_effect=AssertionError("Fallback is unnecessary"))
    monkeypatch.setattr(scraper, "_extract_with_llm", fallback)
    result = scraper.fetch("https://example.ro/item")
    assert result == ScrapeResult(price=0, in_stock=False, title="Structured", currency="EUR")
    fallback.assert_not_called()


def test_blank_metadata_allows_fallback(monkeypatch):
    scraper = PriceScraper()
    html = '''<script type="application/ld+json">
    {"@type":"Product","name":" ","offers":{"priceCurrency":" "}}
    </script><title> </title><meta property="product:price:currency" content=" ">'''
    monkeypatch.setattr(scraper, "_http_get", lambda _url: SimpleNamespace(status_code=200, text=html))
    monkeypatch.setattr(scraper, "_extract_with_llm", Mock(return_value=(10, "Item", "EUR", None)))
    result = scraper.fetch("https://example.ro/item")
    assert result == ScrapeResult(price=10, title="Item", currency="EUR")


@pytest.mark.parametrize("error", [CapacityBusy, CapacityTimeout, CapacityError])
@pytest.mark.parametrize("policy", ["interactive", "background"])
def test_fetch_reports_temporary_capacity_failure(monkeypatch, error, policy):
    scraper = PriceScraper()
    monkeypatch.setattr(scraper, "_http_get", lambda _url: SimpleNamespace(status_code=200, text="<title>Site</title>"))
    query = Mock(side_effect=error("Unavailable"))
    monkeypatch.setattr("wishlist.scraper.query_llm", query)
    result = scraper.fetch("https://example.ro/item", capacity_policy=policy)
    assert result.failure == FAILURE_BUSY
    assert result.has_data is False
    assert query.call_args.kwargs["capacity_policy"] == policy
