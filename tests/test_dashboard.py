"""Focused coverage for the local dashboard and its supporting API routes."""

import importlib

import pytest

import db


@pytest.fixture
def dashboard_client(tmp_db, monkeypatch):
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", "9999")
    api = importlib.import_module("api")
    monkeypatch.setattr(api, "API_TOKEN", None)
    monkeypatch.setattr(api, "_db_initialized", True)
    api.app.config.update(TESTING=True)
    return api, api.app.test_client()


def test_dashboard_and_assets_are_served(dashboard_client):
    _api, client = dashboard_client

    page = client.get("/")
    stylesheet = client.get("/static/dashboard.css")
    script = client.get("/static/dashboard.js")

    assert page.status_code == 200
    assert b"Bot Control" in page.data
    assert b"page-wishlist" in page.data
    assert stylesheet.status_code == 200
    assert b"@media (max-width: 780px)" in stylesheet.data
    assert script.status_code == 200
    assert b'"X-Discord-ID-Format": "string"' in script.data
    assert b"return timestamp / 1000" in script.data
    assert b"if (!ticket.current()) return" in script.data
    assert b"requestVersions: new Map()" in script.data
    assert b"setInterval(loadStats, 15000)" in script.data


def test_new_data_routes_keep_bearer_auth(dashboard_client, monkeypatch):
    api, client = dashboard_client
    monkeypatch.setattr(api, "API_TOKEN", "private-token")

    assert client.get("/system/stats").status_code == 401
    assert client.get("/wishlist/history?user_id=1&url=https://example.com").status_code == 401
    assert client.get("/system/stats", headers={"Authorization": "Bearer private-token"}).status_code == 200


def test_discord_ids_accept_strings_and_round_trip_exactly(dashboard_client):
    _api, client = dashboard_client
    user_id = "1234567890123456789"
    channel_id = "8876543210987654321"

    created = client.post(
        "/reminders/add",
        json={
            "user_id": user_id,
            "channel_id": channel_id,
            "remind_at": 2_000_000_000,
            "message": "Exact snowflakes",
        },
    )
    exact = client.get(
        "/reminders/all", headers={"X-Discord-ID-Format": "string"}
    ).get_json()[0]
    legacy = client.get("/reminders/all").get_json()[0]

    assert created.status_code == 200
    assert exact["user_id"] == user_id
    assert exact["channel_id"] == channel_id
    assert legacy["user_id"] == int(user_id)
    assert legacy["channel_id"] == int(channel_id)


def test_wishlist_history_is_ordered_scoped_and_handles_empty(dashboard_client):
    _api, client = dashboard_client
    owner_id = 1234567890123456789
    url = "https://shop.example/item"
    item_id = db.add_scraped_item(
        owner_id, url, title="Desk lamp", price=None, stock=True, currency="RON"
    )

    empty = client.get(
        f"/wishlist/history?user_id={owner_id}&url={url}",
        headers={"X-Discord-ID-Format": "string"},
    )
    assert empty.status_code == 200
    assert empty.get_json()["history"] == []
    assert empty.get_json()["item"]["user_id"] == str(owner_id)

    with db._connect(commit=True) as cursor:
        cursor.execute(
            "INSERT INTO price_history (item_id, price, timestamp) VALUES (?, ?, ?)",
            (item_id, 120, 200),
        )
        cursor.execute(
            "INSERT INTO price_history (item_id, price, timestamp) VALUES (?, ?, ?)",
            (item_id, 100, 100),
        )

    response = client.get(f"/wishlist/history?user_id={owner_id}&url={url}")
    assert response.status_code == 200
    assert response.get_json()["history"] == [
        {"price": 100.0, "timestamp": 100.0},
        {"price": 120.0, "timestamp": 200.0},
    ]
    assert client.get(f"/wishlist/history?user_id=9&url={url}").status_code == 404
    assert client.get(
        f"/wishlist/history?user_id={owner_id}&url=https://shop.example/missing"
    ).status_code == 404


def test_system_stats_returns_structured_metrics(dashboard_client, monkeypatch):
    api, client = dashboard_client

    class Memory:
        total = 1_000
        used = 400
        percent = 40.0

    class Disk:
        total = 2_000
        used = 500
        percent = 25.0

    monkeypatch.setattr(api.psutil, "cpu_percent", lambda interval: 12.5)
    monkeypatch.setattr(api.psutil, "virtual_memory", lambda: Memory())
    monkeypatch.setattr(api.psutil, "disk_usage", lambda _path: Disk())
    monkeypatch.setattr(api.psutil, "boot_time", lambda: api.time.time() - 300)

    stats = client.get("/system/stats").get_json()

    assert stats["cpu_percent"] == 12.5
    assert stats["memory"] == {"total": 1_000, "used": 400, "percent": 40.0}
    assert stats["disk"]["percent"] == 25.0
    assert 299 <= stats["uptime_seconds"] <= 301
    assert stats["timezone"]


def test_system_stats_uses_null_for_unavailable_metrics(dashboard_client, monkeypatch):
    api, client = dashboard_client

    def unavailable(*_args, **_kwargs):
        raise OSError("not exposed")

    monkeypatch.setattr(api.psutil, "cpu_percent", unavailable)
    monkeypatch.setattr(api.psutil, "virtual_memory", unavailable)
    monkeypatch.setattr(api.psutil, "disk_usage", unavailable)
    monkeypatch.setattr(api.psutil, "boot_time", unavailable)

    stats = client.get("/system/stats").get_json()

    assert stats["cpu_percent"] is None
    assert stats["memory"] is None
    assert stats["disk"] is None
    assert stats["uptime_seconds"] is None
