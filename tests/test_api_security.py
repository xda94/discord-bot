import logging
import json
import sqlite3
from unittest.mock import Mock

import pytest
import requests

import db
from flight_provider import SerpApiFlightProvider
from logger import RedactingFormatter, redact_sensitive
from web.app import create_app


@pytest.fixture
def secured_client(tmp_db):
    client = create_app({"TESTING": True, "API_TOKEN": "test-token"}).test_client()
    client.environ_base["HTTP_AUTHORIZATION"] = "Bearer test-token"
    return client


@pytest.mark.parametrize("token", [None, "", "   "])
def test_unconfigured_token_blocks_protected_routes(tmp_db, token):
    client = create_app({"TESTING": True, "API_TOKEN": token}).test_client()
    assert client.get("/birthdays").status_code == 503
    assert client.post("/jokes", json={"text": "unauthorized"}).status_code == 503
    assert db.get_all_jokes() == []
    assert client.get("/").status_code == 200
    assert client.get("/static/dashboard.js").status_code == 200
    assert client.get("/health").get_json() == {"status": "ok", "database": "ok"}


def test_unicode_authorization_is_rejected_without_error(secured_client):
    response = secured_client.post(
        "/jokes", json={"text": "unauthorized"},
        headers={"Authorization": "Bearer invalid-ș"},
    )
    assert response.status_code == 401
    assert db.get_all_jokes() == []


@pytest.mark.parametrize("payload", [[], "value", None, {"value": float("nan")}, {"value": float("inf")}, {"value": 10 ** 500}])
def test_invalid_json_values_never_mutate_settings(secured_client, payload):
    response = secured_client.put("/settings/custom", data=json.dumps(payload), content_type="application/json")
    assert response.status_code == 400
    assert db.get_setting("custom") is None


def test_malformed_json_never_mutates_settings(secured_client):
    response = secured_client.put("/settings/custom", data='{"value":', content_type="application/json")
    assert response.status_code == 400
    assert db.get_setting("custom") is None


@pytest.mark.parametrize("key,value", [
    ("mention_model", "unapproved-model"), ("sponsor_set_at", "nan"),
    ("sponsor_set_at", -1), ("sponsor_warned", "true"),
    ("sponsor_tier", "unknown"), ("joke_channel_id", -1),
    ("joke_send_time", "25:00"), ("joke_last_sent_date", "tomorrow"),
    ("custom", {"nested": "value"}),
])
def test_invalid_known_settings_preserve_prior_value(secured_client, key, value):
    db.set_setting(key, "original")
    response = secured_client.put(f"/settings/{key}", json={"value": value})
    assert response.status_code == 400
    assert db.get_setting(key) == "original"


def test_custom_scalar_setting_remains_supported(secured_client):
    assert secured_client.put("/settings/custom", json={"value": "configured"}).status_code == 200
    assert db.get_setting("custom") == "configured"


def test_health_checks_existing_database_without_exposing_error(secured_client, monkeypatch):
    assert secured_client.get("/health").status_code == 200
    monkeypatch.setattr(db.connection, "_connect", Mock(side_effect=sqlite3.OperationalError("private database path")))
    response = secured_client.get("/health")
    assert response.status_code == 503
    assert response.get_json() == {"status": "unavailable", "database": "unavailable"}
    assert b"private" not in response.data


def test_health_reports_initialization_failure(monkeypatch):
    monkeypatch.setattr(db, "init_db", Mock(side_effect=sqlite3.OperationalError("private path")))
    client = create_app({"TESTING": True, "API_TOKEN": None}).test_client()
    response = client.get("/health")
    assert response.status_code == 503
    assert response.get_json() == {"status": "unavailable", "database": "unavailable"}


@pytest.mark.parametrize("payload", [
    {"target_price": float("nan"), "target_currency": "EUR"},
    {"target_price": float("inf"), "target_currency": "EUR"},
    {"clear_target": True, "restock_only": "invalid"},
])
def test_invalid_wishlist_preferences_preserve_target(secured_client, payload):
    url = "https://shop.example/item"
    db.add_scraped_item(7, url, price=20, currency="EUR")
    db.set_scraped_item_target(7, url, 15, "EUR")
    original = db.get_scraped_item(7, url)
    response = secured_client.put("/wishlist/preferences", json={"user_id": 7, "url": url, **payload})
    assert response.status_code == 400
    assert db.get_scraped_item(7, url) == original


def test_failed_atomic_wishlist_preferences_returns_error(secured_client, monkeypatch):
    from web.routes import wishlist

    url = "https://shop.example/item"
    db.add_scraped_item(7, url, price=20, currency="EUR")
    update = Mock(return_value=False)
    monkeypatch.setattr(wishlist, "update_scraped_item_preferences", update)
    response = secured_client.put("/wishlist/preferences", json={
        "user_id": 7, "url": url, "target_price": 15,
        "target_currency": "EUR", "restock_only": True,
    })
    assert response.status_code == 500
    update.assert_called_once_with(7, url, target_price=15.0, target_currency="EUR", restock_only=True)


def test_failed_refresh_persistence_returns_error(secured_client, monkeypatch):
    from web.routes import wishlist
    from wishlist.scraper import ScrapeResult

    url = "https://shop.example/item"
    db.add_scraped_item(7, url, price=20, currency="EUR")
    monkeypatch.setattr(wishlist, "refresh_item", Mock(return_value=ScrapeResult(failure="database-error")))
    response = secured_client.post("/wishlist/refresh", json={"user_id": 7, "url": url})
    assert response.status_code == 500


def test_credential_validation_response_redacts_provider_key(secured_client, monkeypatch):
    from web.routes import flights

    key = "private-user-api-key"
    session = Mock()
    session.get.side_effect = requests.RequestException(f"https://serpapi.com/account.json?api_key={key}")
    provider = SerpApiFlightProvider(api_key=key, session=session)
    monkeypatch.setattr(flights, "SerpApiFlightProvider", Mock(return_value=provider))
    response = secured_client.post("/flights/credentials", json={"user_id": 7, "api_key": key})
    assert response.status_code == 400
    assert key not in response.get_data(as_text=True)
    assert "REDACTED" in response.get_json()["detail"]


def test_log_formatter_redacts_secrets_and_exception_urls(monkeypatch):
    monkeypatch.setenv("LLAMA_CPP_API_KEY", "private-llama-key")
    try:
        raise requests.RequestException("failed https://provider.example?api_key=private-user-key")
    except requests.RequestException:
        import sys
        exc_info = sys.exc_info()
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, "Authorization Bearer %s", ("private-llama-key",), exc_info)
    output = RedactingFormatter("%(message)s").format(record)
    assert "private-llama-key" not in output
    assert "private-user-key" not in output
    assert "RequestException" in output
    assert "provider.example" in output


@pytest.mark.parametrize("message", [
    '{"api_key": "private key with spaces"}',
    "{'password': 'private key with spaces'}",
    'https://private-user:private-password@provider.example/endpoint',
    '{"api_key": "private\\\"key"}',
])
def test_redaction_covers_quoted_values_and_url_credentials(message):
    output = redact_sensitive(message)
    assert "private" not in output
    assert "[REDACTED]" in output
