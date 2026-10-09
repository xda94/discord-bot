"""Tests for the data/config-only Flask routes.

These deliberately avoid network-backed routes: provider credential validation
and wishlist refresh are integration concerns, while the API contract and the
DB mutations they authorize are covered here.
"""

import importlib
from unittest.mock import Mock

import pytest

import db


@pytest.fixture
def client(tmp_db, monkeypatch):
    # api.py validates process settings at import time. Build a fresh test app
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", "9999")
    api = importlib.import_module("api")
    app = api.create_app({"TESTING": True, "API_TOKEN": "test-token"})
    client = app.test_client()
    client.environ_base["HTTP_AUTHORIZATION"] = "Bearer test-token"
    return client


def test_keyword_add_success_is_visible(client):
    response = client.post(
        "/keywords/add", json={"keyword": "Hello", "response": "world", "guild_id": 20}
    )

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
    assert client.get("/keywords/get?guild_id=20").get_json() == {"hello": ["world"]}


def test_keyword_add_database_failure_is_server_error(client, monkeypatch):
    from web.routes import administration

    add_response = Mock(return_value=False)
    monkeypatch.setattr(administration, "add_response", add_response)

    response = client.post(
        "/keywords/add", json={"keyword": "hello", "response": "world", "guild_id": 20}
    )

    add_response.assert_called_once_with("hello", "world", 20)
    assert response.status_code == 500
    assert response.get_json() == {"error": "Failed to save keyword"}
    assert db.get_all_responses(20) == {}


def test_keyword_analytics_can_be_filtered_to_requester(client):
    db.log_keyword_usage("Hello", 10, 20)
    db.log_keyword_usage("hello", 10, 20)
    db.log_keyword_usage("other", 11, 20)

    response = client.get("/keywords/top?guild_id=20&user_id=10")

    assert response.status_code == 200
    assert response.get_json() == {
        "guild_id": 20,
        "user_id": 10,
        "keywords": [{"keyword": "hello", "count": 2}],
    }


def test_wishlist_add_success_saves_item_and_history(client, monkeypatch):
    from web.routes import wishlist
    from wishlist.scraper import ScrapeResult

    url = "https://shop.example/item"
    monkeypatch.setattr(wishlist._price_scraper, "fetch", Mock(return_value=ScrapeResult(
        price=10, title="Item", in_stock=False, currency="EUR"
    )))

    response = client.post("/wishlist/add", json={"user_id": 7, "url": url})

    assert response.status_code == 201
    assert response.get_json()["status"] == "ok"
    saved = db.get_scraped_item(7, url)
    assert response.get_json()["id"] == saved[0]
    assert saved[3:7] == (10, 0, "Item", "EUR")
    assert [row[0] for row in db.get_price_history(7, url)] == [10]


def test_wishlist_add_duplicate_is_conflict_without_changing_saved_item(client, monkeypatch):
    from web.routes import wishlist
    from wishlist.scraper import ScrapeResult

    url = "https://shop.example/item"
    item_id = db.add_scraped_item(7, url, title="Saved", price=20, stock=True, currency="RON")
    db.add_price_history(item_id, 20)
    original = db.get_scraped_item(7, url)
    history = db.get_price_history(7, url)
    monkeypatch.setattr(wishlist._price_scraper, "fetch", Mock(return_value=ScrapeResult(price=10)))

    response = client.post("/wishlist/add", json={"user_id": 7, "url": url})

    assert response.status_code == 409
    assert response.get_json() == {"error": "Already tracked"}
    assert db.get_scraped_item(7, url) == original
    assert db.get_price_history(7, url) == history


def test_wishlist_add_database_failure_is_server_error_without_history(client, monkeypatch):
    from web.routes import wishlist
    from wishlist.scraper import ScrapeResult

    url = "https://shop.example/item"
    monkeypatch.setattr(wishlist._price_scraper, "fetch", Mock(return_value=ScrapeResult(price=10)))
    add_item = Mock(return_value=False)
    monkeypatch.setattr(wishlist, "add_scraped_item", add_item)

    response = client.post("/wishlist/add", json={"user_id": 7, "url": url})

    assert response.status_code == 500
    assert response.get_json() == {"error": "Failed to save wishlist item"}
    add_item.assert_called_once()
    assert add_item.call_args.kwargs["record_history"] is True
    assert db.get_scraped_item(7, url) is None


def test_llm_feedback_summary_and_mention_model_config(client):
    assert db.track_llm_response(
        101, 7, "mention", guild_id=88, model="discord-bot", prompt_version="v1"
    )
    assert db.track_llm_response(
        102, 7, "mention", guild_id=88, model="discord-bot", prompt_version="v1"
    )
    assert db.set_llm_response_rating(101, 7, 1)
    assert db.set_llm_response_rating(102, 7, -1)

    summary = client.get("/llm/feedback/summary?guild_id=88")
    assert summary.status_code == 200
    assert summary.get_json()["groups"] == [
        {
            "category": "mention",
            "model": "discord-bot",
            "prompt_version": "v1",
            "ratings": 2,
            "up": 1,
            "down": 1,
            "approval_percent": 50.0,
            "ready_to_compare": False,
        }
    ]

    update = client.put("/llm/mention-model", json={"model": "other-model"})
    assert update.status_code == 200
    assert client.get("/llm/mention-model").get_json()["model"] == "other-model"


def test_inactivity_and_wishlist_preferences_are_configurable(client):
    inactivity = client.put("/inactivity/guilds/9", json={"enabled": False})
    assert inactivity.status_code == 200
    assert client.get("/inactivity/guilds/9").get_json() == {
        "guild_id": 9,
        "enabled": False,
    }

    url = "https://shop.example/product"
    db.add_scraped_item(7, url, price=500, stock=False, currency="RON")
    update = client.put(
        "/wishlist/preferences",
        json={
            "user_id": 7,
            "url": url,
            "target_price": 400,
            "target_currency": "ron",
            "restock_only": True,
        },
    )
    assert update.status_code == 200
    body = update.get_json()
    assert body["target_price"] == 400.0
    assert body["target_currency"] == "RON"
    assert body["restock_only"] is True


def test_birthday_dashboard_api_crud_and_validation(client):
    user_id = "1234567890123456789"
    channel_id = "2234567890123456789"
    guild_id = "3234567890123456789"
    exact_headers = {"X-Discord-ID-Format": "string"}

    saved = client.put(
        f"/birthdays/{user_id}",
        json={
            "channel_id": channel_id,
            "guild_id": guild_id,
            "month": 12,
            "day": 25,
        },
    )
    assert saved.status_code == 200
    assert client.get("/birthdays", headers=exact_headers).get_json() == [
        {
            "user_id": user_id,
            "channel_id": channel_id,
            "guild_id": guild_id,
            "month": 12,
            "day": 25,
            "last_sent_year": None,
        }
    ]

    invalid = client.put(
        f"/birthdays/{user_id}",
        json={"channel_id": channel_id, "guild_id": None, "month": 4, "day": 31},
    )
    assert invalid.status_code == 400
    assert client.get("/birthdays", headers=exact_headers).get_json()[0]["month"] == 12

    deleted = client.delete(f"/birthdays/{user_id}")
    assert deleted.status_code == 200
    assert client.get("/birthdays").get_json() == []
    assert client.delete(f"/birthdays/{user_id}").status_code == 404


def test_flight_tracker_requires_preconfigured_credentials(client):
    response = client.post(
        "/flights/trackers",
        json={
            "user_id": 7,
            "origin": "otp",
            "destination": "bkk",
            "start_date": "2030-06-01",
            "end_date": "2030-06-10",
        },
    )
    assert response.status_code == 409
    assert "credentials" in response.get_json()["error"].lower()
    assert client.get("/flights/trackers?user_id=7").get_json() == {
        "user_id": 7,
        "trackers": [],
    }


def test_sponsor_tier_routes_create_update_and_guard_generic_setting(client):
    listed = client.get("/sponsors/tiers")
    assert listed.status_code == 200
    assert [tier["id"] for tier in listed.get_json()] == [
        "standard", "entuziast", "premium", "ultra"
    ]

    created = client.post(
        "/sponsors/tiers",
        json={"name": "Community", "price_per_year": "12.50", "chance": 0.125},
    )
    assert created.status_code == 201
    tier = created.get_json()
    assert tier["price_per_year"] == "12.50"
    assert tier["chance"] == 0.125

    updated = client.put(
        f"/sponsors/tiers/{tier['id']}",
        json={"name": "Community Plus", "price_per_year": "0", "chance": 1},
    )
    assert updated.status_code == 200
    assert updated.get_json()["id"] == tier["id"]
    assert updated.get_json()["name"] == "Community Plus"

    duplicate = client.post(
        "/sponsors/tiers",
        json={"name": " sponsor   standard ", "price_per_year": "1", "chance": 0},
    )
    assert duplicate.status_code == 409
    assert client.post("/sponsors/tiers", json={"name": "missing"}).status_code == 400
    assert client.put(
        "/settings/sponsor_tiers", json={"value": "do not replace typed data"}
    ).status_code == 400
