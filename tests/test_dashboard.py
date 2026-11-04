"""Focused coverage for the local dashboard and its supporting API routes."""

import importlib
import xml.etree.ElementTree as ET

import pytest

import db


@pytest.fixture
def dashboard_client(tmp_db, monkeypatch):
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", "9999")
    api = importlib.import_module("api")
    from web.routes import administration

    app = api.create_app({"TESTING": True, "API_TOKEN": "test-token"})
    client = app.test_client()
    client.environ_base["HTTP_AUTHORIZATION"] = "Bearer test-token"
    return administration, client


def test_dashboard_and_assets_are_served(dashboard_client):
    _api, client = dashboard_client

    page = client.get("/")
    stylesheet = client.get("/static/dashboard.css")
    script = client.get("/static/dashboard.js")
    translations = client.get("/static/dashboard-i18n.js")

    assert page.status_code == 200
    assert b"Bot Control" in page.data
    assert b"Sign in to Bot Control" in page.data
    assert b'id="login-form"' in page.data
    assert b"page-wishlist" in page.data
    assert b"page-memory" not in page.data
    assert b"page-birthdays" in page.data
    assert b"page-users" in page.data
    assert b'id="users-scope"' in page.data
    assert b'id="users-list"' in page.data
    assert b"page-community" not in page.data
    assert b"profile-form" not in page.data
    assert b"sponsor-tier-form" in page.data
    assert b"sponsor-tiers-list" in page.data
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
    assert b'data-metric="temperature"' in page.data
    assert b"data.temperature_celsius" in script.data
    assert b'class="keyword-groups"' in script.data
    assert b'class="keyword-group"' in script.data
    assert b"Delete response" in script.data
    assert b"Delete keyword" in script.data
    assert b">Delete all<" not in script.data
    assert b".keyword-response-text" in stylesheet.data
    assert b'api("/birthdays")' in script.data
    assert b"birthdayParts" in script.data
    assert b"/sponsors/tiers" in script.data
    assert b"formatChancePercent" in script.data
    assert b"/interactions" in script.data
    assert b"open-user-feature" in script.data
    assert b"/analytics/delivery" in script.data
    assert b"/analytics/natural" not in script.data
    assert translations.status_code == 200


def test_birthday_navigation_uses_decorative_outline_icon(dashboard_client):
    _api, client = dashboard_client
    html = client.get("/").get_data(as_text=True)
    stylesheet = client.get("/static/dashboard.css").get_data(as_text=True)
    button_content = html.split(
        '<button class="nav-item" data-page="birthdays">', 1
    )[1].split("</button>", 1)[0]
    button = ET.fromstring(f"<button>{button_content}</button>")
    icon_slot = button.find("span")
    icon = icon_slot.find("{http://www.w3.org/2000/svg}svg")

    assert "".join(button.itertext()) == "Birthdays"
    assert "🎂" not in button_content
    assert icon_slot.attrib["class"] == "birthday-icon"
    assert icon.attrib["viewBox"] == "0 0 24 24"
    assert icon.attrib["fill"] == "none"
    assert icon.attrib["stroke"] == "currentColor"
    assert icon.attrib["aria-hidden"] == "true"
    assert icon.attrib["focusable"] == "false"
    assert icon.find("{http://www.w3.org/2000/svg}rect") is not None
    assert icon.find("{http://www.w3.org/2000/svg}path") is not None
    assert ".nav-item .birthday-icon { height: 20px;" in stylesheet
    assert "flex: 0 0 20px;" in stylesheet
    assert ".nav-item .birthday-icon svg { display: block; width: 18px; height: 18px; }" in stylesheet
    navigation_rule = stylesheet.split(".nav-item {", 1)[1].split("}", 1)[0]
    assert "color: #a7aebe;" in navigation_rule
    assert ".nav-item:hover { color: var(--text);" in stylesheet
    assert ".nav-item.active { color: white;" in stylesheet
    for selector in (".nav-item .birthday-icon {", ".nav-item .birthday-icon svg {"):
        assert "color:" not in stylesheet.split(selector, 1)[1].split("}", 1)[0]
    mobile_rules = stylesheet.split("@media (max-width: 780px) {", 1)[1]
    assert ".sidebar { transform: translateX(-100%);" in mobile_rules
    assert "body.menu-open .sidebar { transform: translateX(0); }" in mobile_rules
    assert ".menu-button { display: block; }" in mobile_rules


def test_new_data_routes_keep_bearer_auth(dashboard_client, monkeypatch):
    _administration, client = dashboard_client
    client.application.config["API_TOKEN"] = "private-token"

    assert client.get("/system/stats").status_code == 401
    assert client.get("/wishlist/history?user_id=1&url=https://example.com").status_code == 401
    assert client.get("/birthdays").status_code == 401
    assert client.get("/interactions").status_code == 401
    assert client.get("/system/stats", headers={"Authorization": "Bearer private-token"}).status_code == 200


def test_interactions_route_filters_scope_and_formats_ids(dashboard_client):
    from db.analytics import record_feature_interaction

    _api, client = dashboard_client
    record_feature_interaction(
        1234567890123456789, "wishlist", "wishlist-show",
        guild_id=8876543210987654321, user_name="Ana", guild_name="Home", occurred_at=100,
    )
    record_feature_interaction(
        1234567890123456789, "flights", "flight-tracker-add", user_name="Ana", occurred_at=200,
    )

    exact = client.get("/interactions", headers={"X-Discord-ID-Format": "string"}).get_json()
    direct = client.get("/interactions?scope=dm").get_json()
    invalid = client.get("/interactions?scope=bad")

    assert [item["feature"] for item in exact["interactions"]] == ["flights", "wishlist"]
    assert exact["interactions"][1]["user_id"] == "1234567890123456789"
    assert exact["interactions"][1]["guild_id"] == "8876543210987654321"
    assert direct["interactions"] == [{
        "user_id": 1234567890123456789, "feature": "flights", "scope_type": "dm",
        "guild_id": None, "user_name": "Ana", "guild_name": "", "count": 1,
        "first_at": 200.0, "last_at": 200.0, "last_action": "flight-tracker-add",
    }]
    assert invalid.status_code == 400
    assert "error" in invalid.get_json()


def test_delivery_analytics_replace_removed_admin_routes(dashboard_client):
    _api, client = dashboard_client

    delivery = client.get("/analytics/delivery")

    assert delivery.status_code == 200
    assert delivery.get_json() == {"reminders": {}, "notifications": {}}
    assert client.get("/analytics/natural").status_code == 404
    assert client.get("/assistant-profiles/1").status_code == 404


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
    monkeypatch.setattr(
        api.psutil,
        "sensors_temperatures",
        lambda: {"gpu": [type("Reading", (), {"current": 70.0})()], "coretemp": [type("Reading", (), {"current": 52.5})()]},
        raising=False,
    )
    monkeypatch.setattr(api.psutil, "virtual_memory", lambda: Memory())
    monkeypatch.setattr(api.psutil, "disk_usage", lambda _path: Disk())
    monkeypatch.setattr(api.psutil, "boot_time", lambda: api.time.time() - 300)

    stats = client.get("/system/stats").get_json()

    assert stats["cpu_percent"] == 12.5
    assert stats["temperature_celsius"] == 52.5
    assert stats["memory"] == {"total": 1_000, "used": 400, "percent": 40.0}
    assert stats["disk"]["percent"] == 25.0
    assert 299 <= stats["uptime_seconds"] <= 301
    assert stats["timezone"]


def test_system_stats_uses_null_for_unavailable_metrics(dashboard_client, monkeypatch):
    api, client = dashboard_client

    def unavailable(*_args, **_kwargs):
        raise OSError("not exposed")

    monkeypatch.setattr(api.psutil, "cpu_percent", unavailable)
    monkeypatch.setattr(api.psutil, "sensors_temperatures", unavailable, raising=False)
    monkeypatch.setattr(api.psutil, "virtual_memory", unavailable)
    monkeypatch.setattr(api.psutil, "disk_usage", unavailable)
    monkeypatch.setattr(api.psutil, "boot_time", unavailable)

    stats = client.get("/system/stats").get_json()

    assert stats["cpu_percent"] is None
    assert stats["temperature_celsius"] is None
    assert stats["memory"] is None
    assert stats["disk"] is None
    assert stats["uptime_seconds"] is None
