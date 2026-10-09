import asyncio
import socket
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import db
from db.connection import _connect
from db.wishlist import get_exchange_rates, set_exchange_rates, update_scraped_item_preferences
from features.wishlist import WishlistFeature
from wishlist.currency import CurrencyConverter
from wishlist.refresh import refresh_item
from wishlist.scraper import FAILURE_DATABASE, PriceScraper, ScrapeResult, _is_valid_http_url


def _addresses(*ips):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443)) for ip in ips]


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/item", "http://10.0.0.1/item", "http://169.254.169.254/",
    "https://[::1]/", "https://[::ffff:127.0.0.1]/", "https://[fc00::1]/",
    "http://localhost/", "https://host.local/", "http://2130706433/",
    "https://user:secret@example.com/", "https://example.com:0/",
    "https://example.com:65536/", "https://example.com\\@localhost/",
    "https://example.com/\nlocalhost", "https://224.0.0.1/", "https://192.0.2.1/",
    "https://192.0.0.8/", "https://[2002:a00:1::]/",
])
def test_unsafe_url_syntax_is_rejected(url):
    assert not _is_valid_http_url(url)


@pytest.mark.parametrize("ips", [("127.0.0.1",), ("93.184.216.34", "10.0.0.1")])
def test_dns_requires_every_address_to_be_public(monkeypatch, ips):
    monkeypatch.setattr("wishlist.scraper.socket.getaddrinfo", lambda *_args, **_kwargs: _addresses(*ips))
    transport = Mock()
    monkeypatch.setattr("wishlist.scraper.impersonated_http", SimpleNamespace(get=transport))
    monkeypatch.setattr("wishlist.scraper._IMPERSONATION_AVAILABLE", True)

    with pytest.raises(ValueError, match="non-public"):
        PriceScraper()._http_get("https://shop.example/item")

    transport.assert_not_called()


def test_curl_request_pins_checked_dns_and_disables_proxy_and_redirects(monkeypatch):
    from curl_cffi.const import CurlOpt

    resolve = Mock(side_effect=[_addresses("93.184.216.34"), _addresses("127.0.0.1")])
    monkeypatch.setattr("wishlist.scraper.socket.getaddrinfo", resolve)
    def get(url, **kwargs):
        assert url == "https://shop.example/item"
        assert kwargs["allow_redirects"] is False
        assert kwargs["curl_options"][CurlOpt.RESOLVE] == ["shop.example:443:93.184.216.34"]
        assert kwargs["curl_options"][CurlOpt.PROXY] == ""
        kwargs["content_callback"](b"<title>Item</title>")
        return SimpleNamespace(status_code=200, content=b"", headers={})
    monkeypatch.setattr("wishlist.scraper.impersonated_http", SimpleNamespace(get=get))
    monkeypatch.setattr("wishlist.scraper._IMPERSONATION_AVAILABLE", True)

    response = PriceScraper()._http_get("https://shop.example/item")

    assert response.content == b"<title>Item</title>"
    assert resolve.call_count == 1


@pytest.mark.parametrize("location", ["http://127.0.0.1/admin", "file:///etc/passwd", "https://name:secret@example.com/", "https://internal.example/"])
def test_redirect_target_is_checked_before_fetch(monkeypatch, location):
    def resolve(host, *_args, **_kwargs):
        return _addresses("127.0.0.1" if host == "internal.example" else "93.184.216.34")
    monkeypatch.setattr("wishlist.scraper.socket.getaddrinfo", resolve)
    response = SimpleNamespace(status_code=302, headers={"Location": location}, close=Mock())
    transport = Mock(return_value=response)
    monkeypatch.setattr("wishlist.scraper.impersonated_http", SimpleNamespace(get=transport))
    monkeypatch.setattr("wishlist.scraper._IMPERSONATION_AVAILABLE", True)

    with pytest.raises(ValueError):
        PriceScraper()._http_get("https://shop.example/item")

    assert transport.call_count == 1
    response.close.assert_called_once()


def test_plain_https_transport_connects_to_checked_ip_with_original_tls_name(monkeypatch):
    resolve = Mock(side_effect=[_addresses("93.184.216.34"), _addresses("127.0.0.1")])
    monkeypatch.setattr("wishlist.scraper.socket.getaddrinfo", resolve)
    monkeypatch.setattr("wishlist.scraper._IMPERSONATION_AVAILABLE", False)
    raw = SimpleNamespace(status=200, headers={"Content-Type": "text/html; charset=utf-8"}, read=Mock(return_value=b"hello"), close=Mock())
    pool = Mock()
    pool.__enter__ = Mock(return_value=pool)
    pool.__exit__ = Mock(return_value=False)
    pool.request.return_value = raw
    factory = Mock(return_value=pool)
    monkeypatch.setattr("wishlist.scraper.urllib3.HTTPSConnectionPool", factory)

    response = PriceScraper()._http_get("https://shop.example/item?q=1")

    assert response.text == "hello"
    assert factory.call_args.args == ("93.184.216.34", 443)
    assert factory.call_args.kwargs["server_hostname"] == "shop.example"
    assert factory.call_args.kwargs["assert_hostname"] == "shop.example"
    assert pool.request.call_args.args == ("GET", "/item?q=1")
    assert pool.request.call_args.kwargs["redirect"] is False
    assert pool.request.call_args.kwargs["preload_content"] is False
    assert resolve.call_count == 1
    raw.close.assert_called_once()


def test_plain_transport_rejects_oversize_body_and_closes_response(monkeypatch):
    monkeypatch.setattr("wishlist.scraper.socket.getaddrinfo", lambda *_args, **_kwargs: _addresses("93.184.216.34"))
    monkeypatch.setattr("wishlist.scraper._IMPERSONATION_AVAILABLE", False)
    monkeypatch.setattr(PriceScraper, "MAX_PAGE_BYTES", 4)
    raw = SimpleNamespace(read=Mock(return_value=b"12345"), close=Mock())
    pool = Mock()
    pool.__enter__ = Mock(return_value=pool)
    pool.__exit__ = Mock(return_value=False)
    pool.request.return_value = raw
    monkeypatch.setattr("wishlist.scraper.urllib3.HTTPConnectionPool", Mock(return_value=pool))

    with pytest.raises(ValueError, match="download limit"):
        PriceScraper()._http_get("http://shop.example/item")

    raw.close.assert_called_once()


def test_plain_transport_follows_public_relative_redirect(monkeypatch):
    monkeypatch.setattr("wishlist.scraper.socket.getaddrinfo", lambda *_args, **_kwargs: _addresses("93.184.216.34"))
    monkeypatch.setattr("wishlist.scraper._IMPERSONATION_AVAILABLE", False)
    raw_redirect = SimpleNamespace(status=302, headers={"Location": "/new"}, read=Mock(return_value=b""), close=Mock())
    raw_page = SimpleNamespace(status=200, headers={}, read=Mock(return_value=b"product"), close=Mock())
    pool = Mock()
    pool.__enter__ = Mock(return_value=pool)
    pool.__exit__ = Mock(return_value=False)
    pool.request.side_effect = [raw_redirect, raw_page]
    monkeypatch.setattr("wishlist.scraper.urllib3.HTTPConnectionPool", Mock(return_value=pool))

    response = PriceScraper()._http_get("http://shop.example/old")

    assert response.text == "product"
    assert pool.request.call_args.args == ("GET", "/new")
    raw_redirect.close.assert_called_once()
    raw_page.close.assert_called_once()


def test_curl_download_limit_stops_before_accumulating_large_body(monkeypatch):
    from curl_cffi.curl import CURL_WRITEFUNC_ERROR

    monkeypatch.setattr("wishlist.scraper.socket.getaddrinfo", lambda *_args, **_kwargs: _addresses("93.184.216.34"))
    monkeypatch.setattr("wishlist.scraper._IMPERSONATION_AVAILABLE", True)
    monkeypatch.setattr(PriceScraper, "MAX_PAGE_BYTES", 4)
    def get(_url, **kwargs):
        assert kwargs["content_callback"](b"1234") == 4
        assert kwargs["content_callback"](b"5") == CURL_WRITEFUNC_ERROR
        raise ValueError("download limit")
    monkeypatch.setattr("wishlist.scraper.impersonated_http", SimpleNamespace(get=get))

    with pytest.raises(ValueError, match="download limit"):
        PriceScraper()._http_get("https://shop.example/item")


def test_refresh_write_failure_rolls_back_history_and_reports_failure(tmp_db):
    url = "https://shop.example/item"
    item_id = db.add_scraped_item(7, url, title="Old", price=100, currency="RON")
    item = db.get_scraped_item(7, url)
    with _connect(commit=True) as c:
        c.execute("CREATE TRIGGER fail_snapshot BEFORE UPDATE ON scraped_items BEGIN SELECT RAISE(ABORT, 'failed'); END")
    result = ScrapeResult(price=80, in_stock=False, title="New", currency="EUR")

    refreshed = refresh_item(item, SimpleNamespace(fetch=lambda _url: result))

    assert refreshed.failure == FAILURE_DATABASE
    assert db.get_scraped_item(7, url) == item
    assert db.get_price_history(7, url) == []


def test_insert_history_failure_does_not_leave_partially_added_item(tmp_db):
    with _connect(commit=True) as c:
        c.execute("CREATE TRIGGER fail_history BEFORE INSERT ON price_history BEGIN SELECT RAISE(ABORT, 'failed'); END")

    assert db.add_scraped_item(7, "https://shop.example/item", price=100, record_history=True) is False
    assert db.get_user_scraped_items(7) == []


def test_preferences_validate_all_fields_before_single_update(tmp_db):
    url = "https://shop.example/item"
    db.add_scraped_item(7, url, price=100)
    assert update_scraped_item_preferences(7, url, target_price=80, target_currency="eur", restock_only=True)
    assert db.get_scraped_item(7, url)[9:13] == (80, "EUR", 0, 1)

    assert not update_scraped_item_preferences(7, url, target_price=50, target_currency="RON", restock_only="false")
    assert db.get_scraped_item(7, url)[9:13] == (80, "EUR", 0, 1)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True])
def test_db_and_currency_reject_invalid_numbers(tmp_db, value):
    url = "https://shop.example/item"
    item_id = db.add_scraped_item(7, url, price=100)
    assert db.add_scraped_item(7, url + "/bad", price=value) is False
    assert db.set_scraped_item_target(7, url, value, "RON") is False
    assert db.add_price_history(item_id, value) is False
    assert db.update_scraped_item_status(item_id, value, True) is False
    assert db.set_exchange_rate("EUR", value) is False
    converter = CurrencyConverter()
    assert converter.to_currency(value, "EUR", "EUR") is None
    assert converter.format_with_conversions(value, "EUR") == "N/A"


def test_currency_conversions_share_one_rate_table_read(tmp_db, monkeypatch):
    set_exchange_rates({"EUR": 1.0, "RON": 5.0, "DKK": 7.0, "USD": 1.1, "GBP": 0.8})
    load = Mock(wraps=get_exchange_rates)
    monkeypatch.setattr("wishlist.currency.get_exchange_rates", load)
    converter = CurrencyConverter()

    for _ in range(5):
        assert "7.00 DKK" in converter.format_with_conversions(1, "EUR")

    assert load.call_count == 1


@pytest.mark.parametrize("rates", [{"RON": 5}, {"RON": 5, "DKK": 7, "USD": float("nan"), "GBP": 0.8}])
def test_currency_refresh_does_not_partially_save_invalid_table(tmp_db, monkeypatch, rates):
    set_exchange_rates({"EUR": 1, "RON": 4.5, "DKK": 6.5, "USD": 1, "GBP": 0.7})
    before = get_exchange_rates()
    response = SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {"rates": rates})
    monkeypatch.setattr("wishlist.currency.requests.get", lambda *_args, **_kwargs: response)

    assert CurrencyConverter().refresh() is False
    assert get_exchange_rates() == before


def test_currency_refresh_invalidates_cached_table_and_reports_storage_error(tmp_db, monkeypatch):
    rates = {"RON": 5, "DKK": 7, "USD": 1.1, "GBP": 0.8}
    set_exchange_rates({"EUR": 1, "RON": 4})
    converter = CurrencyConverter()
    assert converter.convert(1, "EUR", "RON") == 4
    response = SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {"rates": rates})
    monkeypatch.setattr("wishlist.currency.requests.get", lambda *_args, **_kwargs: response)

    assert converter.refresh() is True
    assert converter.convert(1, "EUR", "RON") == 5
    monkeypatch.setattr("wishlist.currency.set_exchange_rates", lambda _rates: False)
    assert converter.refresh() is False


def test_discord_add_persists_on_worker_thread(tmp_db, monkeypatch):
    feature = object.__new__(WishlistFeature)
    feature.scraper = SimpleNamespace(fetch=lambda _url: ScrapeResult(price=80, in_stock=True))
    loop_thread = threading.get_ident()
    original = db.add_scraped_item
    def persist(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        return original(*args, **kwargs)
    monkeypatch.setattr("features.wishlist.db.add_scraped_item", persist)

    result = asyncio.run(feature.add_item_for_user(7, "https://shop.example/item", language="en"))

    assert result.status == "added"
    assert [row[0] for row in db.get_price_history(7, result.url)] == [80]
