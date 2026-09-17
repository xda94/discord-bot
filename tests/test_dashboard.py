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
    assert b"Sign in to Bot Control" in page.data
    assert b'id="login-form"' in page.data
    assert b"page-wishlist" in page.data
    assert b"page-memory" in page.data
    assert stylesheet.status_code == 200
    assert b"@media (max-width: 780px)" in stylesheet.data
    assert script.status_code == 200
    assert b'"X-Discord-ID-Format": "string"' in script.data
    assert b"headers.Authorization = `Bearer ${state.token}`" in script.data
    assert b'sessionStorage.getItem("bot-dashboard-token")' in script.data
    assert b"response.status === 401" in script.data
    assert b"return timestamp / 1000" in script.data
    assert b"if (!ticket.current()) return" in script.data
    assert b"requestVersions: new Map()" in script.data
    assert b"setInterval(loadStats, 15000)" in script.data
    assert b'class="keyword-groups"' in script.data
    assert b'class="keyword-group"' in script.data
    assert b"Delete response" in script.data
    assert b"Delete keyword" in script.data
    assert b">Delete all<" not in script.data
    assert b".keyword-response-text" in stylesheet.data
    assert b"/memory/channels" in script.data
    assert b"/memory/users/" in script.data


def test_new_data_routes_keep_bearer_auth(dashboard_client, monkeypatch):
    api, client = dashboard_client
    monkeypatch.setattr(api, "API_TOKEN", "private-token")

    assert client.get("/system/stats").status_code == 401
    assert client.get("/wishlist/history?user_id=1&url=https://example.com").status_code == 401
    assert client.get("/memory/channels?guild_id=1").status_code == 401
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


def test_memory_channel_and_user_controls(dashboard_client):
    _api, client = dashboard_client
    guild_id = 1234567890123456789
    channel_id = 2234567890123456789
    user_id = 3234567890123456789
    exact_headers = {"X-Discord-ID-Format": "string"}

    enabled = client.put(
        f"/memory/channels/{guild_id}/{channel_id}", json={"enabled": True}
    )
    assert enabled.status_code == 200
    channels = client.get(
        f"/memory/channels?guild_id={guild_id}", headers=exact_headers
    ).get_json()
    assert channels == {
        "guild_id": str(guild_id),
        "channels": [{"channel_id": str(channel_id), "enabled": True}],
    }

    opted_in = client.put(
        f"/memory/users/{user_id}/preference",
        json={"scope_id": str(guild_id), "enabled": True},
    )
    assert opted_in.status_code == 200
    assert db.apply_llm_memory_delta(
        guild_id,
        user_id,
        ({"kind": "fact", "content": "Likes tea", "source_text": "I like tea"},),
        (),
        (),
    )
    memory = client.get(
        f"/memory/users/{user_id}?scope_id={guild_id}", headers=exact_headers
    ).get_json()
    assert memory["scope_id"] == str(guild_id)
    assert memory["user_id"] == str(user_id)
    assert memory["preference"] is True
    assert memory["entries"][0]["content"] == "Likes tea"
    assert memory["transcript"] == []

    forgotten = client.delete(
        f"/memory/users/{user_id}", json={"scope_id": str(guild_id)}
    )
    assert forgotten.status_code == 200
    after = client.get(f"/memory/users/{user_id}?scope_id={guild_id}").get_json()
    assert after["preference"] is True
    assert after["entries"] == []
    assert after["transcript"] == []


def test_memory_opt_out_and_guild_purge_require_expected_confirmation(dashboard_client):
    _api, client = dashboard_client
    guild_id = 55
    first_user = 7
    second_user = 8
    for user_id in (first_user, second_user):
        assert db.apply_llm_memory_delta(
            guild_id,
            user_id,
            ({"kind": "topic", "content": f"Topic {user_id}", "source_text": "source"},),
            (),
            (),
        )

    opted_out = client.put(
        f"/memory/users/{first_user}/preference",
        json={"scope_id": guild_id, "enabled": False},
    )
    assert opted_out.status_code == 200
    assert opted_out.get_json()["removed"] == 1
    assert db.get_llm_memory_preference(guild_id, first_user) is False
    assert db.get_llm_memory_entries(guild_id, first_user) == []

    assert client.delete(
        f"/memory/guilds/{guild_id}", json={"confirmation": "purge"}
    ).status_code == 400
    purged = client.delete(
        f"/memory/guilds/{guild_id}", json={"confirmation": "PURGE"}
    )
    assert purged.status_code == 200
    assert purged.get_json()["removed_users"] == 1
    assert db.get_llm_memory_entries(guild_id, second_user) == []


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
