"""Tests for the data/config-only Flask routes.

These deliberately avoid network-backed routes: provider credential validation
and wishlist refresh are integration concerns, while the API contract and the
DB mutations they authorize are covered here.
"""

import importlib

import pytest

import db


@pytest.fixture
def client(tmp_db, monkeypatch):
    # api.py validates these settings at import time.  Set explicit test values
    # before its first import, then disable auth so each test can focus on the
    # route contract.
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", "9999")
    api = importlib.import_module("api")
    monkeypatch.setattr(api, "API_TOKEN", None)
    monkeypatch.setattr(api, "_db_initialized", True)
    api.app.config.update(TESTING=True)
    return api.app.test_client()


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
