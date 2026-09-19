"""Focused coverage for deployer-only bot analytics."""

import asyncio
import importlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

import discord
import pytest

import analytics
import db
from features import llm_mention, reminders, wishlist_graphs


def _timestamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


def _set_tracking_start(path, timestamp):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE analytics_metadata SET value = ? WHERE key = 'tracking_started_at'",
            (str(timestamp),),
        )


def test_daily_utc_aggregation_and_server_isolation(tmp_db):
    start = _timestamp("2026-09-18T00:00:00")
    _set_tracking_start(tmp_db, start)
    db.record_analytics_activity(
        "command", "help", guild_id=10, occurred_at=_timestamp("2026-09-18T23:59:59")
    )
    db.record_analytics_activity(
        "command", "help", guild_id=10, occurred_at=_timestamp("2026-09-19T00:00:00")
    )
    db.record_analytics_activity(
        "mention", "bot-mention", guild_id=20, occurred_at=_timestamp("2026-09-19T01:00:00")
    )
    db.record_analytics_activity(
        "processing", "flight-check", scope_type="global", occurred_at=_timestamp("2026-09-19T02:00:00")
    )

    report = db.get_analytics_summary("7d", 10, now=_timestamp("2026-09-19T12:00:00"))

    assert report["category_totals"]["command"] == 2
    assert report["category_totals"]["mention"] == 0
    assert [row["total"] for row in report["daily"]] == [1, 1]
    assert report["scope_totals"] == {"guild": 2, "dm": 0, "global": 0}


def test_atomic_concurrent_increments_persist(tmp_db):
    when = _timestamp("2026-09-19T10:00:00")
    _set_tracking_start(tmp_db, _timestamp("2026-09-19T00:00:00"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(
            lambda _index: db.record_analytics_activity(
                "automatic", "keyword-reply", guild_id=99, occurred_at=when
            ),
            range(12),
        ))

    report = db.get_analytics_summary("all", 99, now=when)
    assert report["category_totals"]["automatic"] == 12
    assert report["features"][0]["count"] == 12


def test_failures_are_reported_separately_from_successful_activity(tmp_db):
    when = _timestamp("2026-09-19T10:00:00")
    _set_tracking_start(tmp_db, _timestamp("2026-09-19T00:00:00"))
    db.record_analytics_activity(
        "scheduled", "reminder-delivery", guild_id=99, occurred_at=when
    )
    db.record_analytics_activity(
        "failure", "reminder-delivery", guild_id=99, occurred_at=when + 1
    )
    db.record_analytics_activity(
        "failure", "command/help", guild_id=99, occurred_at=when + 2
    )

    report = db.get_analytics_summary("all", 99, now=when + 3)

    assert report["category_totals"]["scheduled"] == 1
    assert report["category_totals"]["failure"] == 2
    assert [row["activity"] for row in report["failures"]] == [
        "command/help", "reminder-delivery"
    ]
    assert all(row["category"] != "failure" for row in report["features"])


def test_command_catalog_includes_unused_and_retains_inactive(tmp_db):
    start = _timestamp("2026-09-19T00:00:00")
    _set_tracking_start(tmp_db, start)
    db.refresh_analytics_command_catalog(["zeta", "alpha", "used"], observed_at=start)
    db.record_analytics_activity(
        "command", "used", guild_id=1, occurred_at=start + 60
    )
    db.refresh_analytics_command_catalog(["alpha", "used"], observed_at=start + 120)

    report = db.get_analytics_summary("all", 1, now=start + 180)

    assert [row["name"] for row in report["commands"]["most_used"]] == ["used", "alpha"]
    assert [row["name"] for row in report["commands"]["least_used"]] == ["alpha", "used"]
    assert [row["name"] for row in report["commands"]["unused"]] == ["alpha"]
    assert report["commands"]["inactive"][0]["name"] == "zeta"


def test_empty_report_fills_every_date_and_sorts_ties(tmp_db):
    now = _timestamp("2026-09-19T12:00:00")
    _set_tracking_start(tmp_db, _timestamp("2026-09-17T00:00:00"))
    db.refresh_analytics_command_catalog(["beta", "alpha"], observed_at=now)

    report = db.get_analytics_summary("30d", now=now)

    assert [row["date"] for row in report["daily"]] == [
        "2026-09-17", "2026-09-18", "2026-09-19"
    ]
    assert all(row["total"] == 0 for row in report["daily"])
    assert [row["name"] for row in report["commands"]["most_used"]] == ["alpha", "beta"]


def test_command_tree_counts_invocations_but_not_autocomplete(monkeypatch):
    calls = []

    async def fake_record_for(category, activity, interaction):
        calls.append((category, activity, interaction.type))

    monkeypatch.setattr(analytics, "record_for", fake_record_for)

    async def ignore_base_error(_self, _interaction, _error):
        return None

    monkeypatch.setattr(discord.app_commands.CommandTree, "on_error", ignore_base_error)
    command = SimpleNamespace(
        type=discord.InteractionType.application_command,
        data={"name": "wishlist-show"},
    )
    autocomplete = SimpleNamespace(
        type=discord.InteractionType.autocomplete,
        data={"name": "wishlist-show"},
    )

    assert asyncio.run(analytics.AnalyticsCommandTree.interaction_check(None, command))
    assert asyncio.run(analytics.AnalyticsCommandTree.interaction_check(None, autocomplete))
    tree = object.__new__(analytics.AnalyticsCommandTree)
    asyncio.run(tree.on_error(command, RuntimeError("failed")))
    assert calls == [
        ("command", "wishlist-show", discord.InteractionType.application_command),
        ("failure", "command/wishlist-show", discord.InteractionType.application_command),
    ]


def test_mentions_count_once_and_analytics_failures_do_not_break_messages(monkeypatch):
    calls = []

    async def fake_record_for(category, activity, message):
        calls.append((category, activity, message.guild.id))

    monkeypatch.setattr(analytics, "record_for", fake_record_for)
    message = SimpleNamespace(
        author=SimpleNamespace(bot=False),
        mentions=[SimpleNamespace(id=42), SimpleNamespace(id=42)],
        guild=SimpleNamespace(id=7),
    )
    assert asyncio.run(analytics.record_message_mention(message, 42)) is True
    assert calls == [("mention", "bot-mention", 7)]

    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "record_analytics_activity", fail)
    asyncio.run(analytics.record("automatic", "test", scope_type="global"))


def test_multipart_llm_delivery_counts_once_and_failed_delivery_is_not_counted(monkeypatch):
    recorded = []

    async def fake_record_for(category, activity, value):
        recorded.append((category, activity))

    monkeypatch.setattr(llm_mention, "record_for", fake_record_for)

    class ReplyTarget:
        guild = SimpleNamespace(id=1)

        async def reply(self, *_args, **_kwargs):
            return SimpleNamespace(id=100, guild=self.guild)

    class Channel:
        def __init__(self, fail=False):
            self.fail = fail
            self.sent = 0

        async def send(self, *_args, **_kwargs):
            self.sent += 1
            if self.fail:
                raise RuntimeError("send failed")

    feature = llm_mention.LLMMentionFeature.__new__(llm_mention.LLMMentionFeature)
    feature.feedback = None
    user = SimpleNamespace(id=5, display_name="User", name="User")
    text = "word " * 900
    success_job = llm_mention.AskJob(
        user=user, question="", model="model", channel=Channel(), reply_to=ReplyTarget()
    )
    assert asyncio.run(feature._reply_mention(success_job, text))
    assert recorded == [("automatic", "llm-reply")]

    recorded.clear()
    failed_job = llm_mention.AskJob(
        user=user, question="", model="model", channel=Channel(fail=True), reply_to=ReplyTarget()
    )
    with pytest.raises(RuntimeError):
        asyncio.run(feature._reply_mention(failed_job, text))
    assert recorded == []


def test_scheduled_delivery_counts_only_after_send_succeeds(monkeypatch):
    recorded = []
    deleted = []

    async def fake_record_for(category, activity, value):
        recorded.append((category, activity, value.guild.id))

    class Channel:
        guild = SimpleNamespace(id=44)

        def __init__(self, fail):
            self.fail = fail

        async def send(self, *_args, **_kwargs):
            if self.fail:
                raise RuntimeError("send failed")

    channels = {1: Channel(False), 2: Channel(True)}
    feature = reminders.RemindersFeature.__new__(reminders.RemindersFeature)
    feature.client = SimpleNamespace(get_channel=lambda channel_id: channels[channel_id])
    monkeypatch.setattr(reminders, "record_for", fake_record_for)
    monkeypatch.setattr(db, "get_due_reminders", lambda: [(10, 1, 1, "ok"), (11, 1, 2, "bad")])
    monkeypatch.setattr(db, "delete_reminder", deleted.append)

    asyncio.run(reminders.RemindersFeature._check.coro(feature))

    assert recorded == [
        ("scheduled", "reminder-delivery", 44),
        ("failure", "reminder-delivery", 44),
    ]
    assert deleted == [10, 11]


def test_graph_control_counts_only_after_ownership_check(monkeypatch):
    recorded = []

    async def fake_record_for(category, activity, value):
        recorded.append((category, activity))

    monkeypatch.setattr(wishlist_graphs, "record_for", fake_record_for)

    class Response:
        async def defer(self):
            return None

    class Interaction:
        response = Response()
        followup = SimpleNamespace(send=lambda *_args, **_kwargs: None)

        async def edit_original_response(self, **_kwargs):
            return None

    class FakeView:
        def __init__(self, allowed):
            self.allowed = allowed
            self._lock = asyncio.Lock()
            self.days = 30
            self.percentage = False
            self.build = lambda days, percentage: ("ok", None)

        async def interaction_check(self, _interaction):
            return self.allowed

        def _update_buttons(self):
            return None

    async def exercise(allowed):
        await wishlist_graphs.WishlistGraphView.update_graph(
            FakeView(allowed), Interaction(),
            analytics_activity="wishlist-graph-period-button",
        )

    asyncio.run(exercise(False))
    assert recorded == []

    asyncio.run(exercise(True))
    assert recorded == [("control", "wishlist-graph-period-button")]


@pytest.fixture
def analytics_client(tmp_db, monkeypatch):
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", "9999")
    api = importlib.import_module("api")
    monkeypatch.setattr(api, "_db_initialized", True)
    api.app.config.update(TESTING=True)
    return api, api.app.test_client()


def test_analytics_endpoint_requires_configured_token(analytics_client, monkeypatch):
    api, client = analytics_client
    monkeypatch.setattr(api, "API_TOKEN", None)
    assert client.get("/analytics/summary").status_code == 503

    monkeypatch.setattr(api, "API_TOKEN", "secret")
    assert client.get("/analytics/summary").status_code == 401
    assert client.get(
        "/analytics/summary", headers={"Authorization": "Bearer wrong"}
    ).status_code == 401


def test_analytics_endpoint_validates_filters_and_returns_totals(
    analytics_client, monkeypatch
):
    api, client = analytics_client
    monkeypatch.setattr(api, "API_TOKEN", "secret")
    db.refresh_analytics_command_catalog(["help"])
    db.record_analytics_activity("command", "help", guild_id=123)
    headers = {"Authorization": "Bearer secret", "X-Discord-ID-Format": "string"}

    invalid = client.get("/analytics/summary?period=year", headers=headers)
    response = client.get("/analytics/summary?period=30d&guild_id=123", headers=headers)

    assert invalid.status_code == 400
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["scope"] == {"type": "guild", "guild_id": "123"}
    assert payload["category_totals"]["command"] == 1
    assert payload["commands"]["most_used"][0]["name"] == "help"


def test_dashboard_contains_analytics_page_and_filters(analytics_client, monkeypatch):
    _api, client = analytics_client
    page = client.get("/").data
    script = client.get("/static/dashboard.js").data

    assert b'data-page="analytics"' in page
    assert b'id="analytics-period"' in page
    assert b'id="analytics-command-sort"' in page
    assert b'id="analytics-failures"' in page
    assert b'/analytics/summary?' in script
    assert b'page === "analytics"' in script
