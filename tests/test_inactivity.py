import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
from discord import app_commands

import db
from features.inactivity import InactivityFeature, pick_chatter
from llm.capacity import reserve


def _msg(author_id: int, *, is_bot: bool = False):
    msg = MagicMock()
    msg.author.id = author_id
    msg.author.bot = is_bot
    return msg


def test_pick_chatter_returns_a_human():
    result = pick_chatter([_msg(1), _msg(2), _msg(1)])
    assert result.id in {1, 2}
    assert result.bot is False


def test_pick_chatter_excludes_bots():
    result = pick_chatter([_msg(99, is_bot=True), _msg(5)])
    assert result.id == 5


def test_pick_chatter_none_when_only_bots():
    assert pick_chatter([_msg(98, is_bot=True), _msg(99, is_bot=True)]) is None


def test_pick_chatter_none_when_empty():
    assert pick_chatter([]) is None


def test_send_nudge_sends_generated_message_and_tags_chatter(monkeypatch):
    feature = object.__new__(InactivityFeature)
    feature.client = MagicMock()
    target = MagicMock()
    target.id = 42
    feature._pick_recent_chatter = AsyncMock(return_value=target)
    channel = MagicMock()
    channel.send = AsyncMock()

    monkeypatch.setattr(
        "features.inactivity.resolve_bot_display_name", lambda *args: "Skippy"
    )
    monkeypatch.setattr(
        "features.inactivity.generate_inactivity_message",
        lambda *args, **kwargs: "Ce mai faceți?",
    )

    assert asyncio.run(feature._send_nudge(channel)) is True
    channel.send.assert_awaited_once_with("<@42> Ce mai faceți?")


def test_send_nudge_does_not_use_fallback_when_generation_fails(monkeypatch):
    feature = object.__new__(InactivityFeature)
    feature.client = MagicMock()
    feature._pick_recent_chatter = AsyncMock(return_value=None)
    channel = MagicMock()
    channel.send = AsyncMock()

    monkeypatch.setattr(
        "features.inactivity.resolve_bot_display_name", lambda *args: "Skippy"
    )
    monkeypatch.setattr(
        "features.inactivity.generate_inactivity_message",
        lambda *args, **kwargs: None,
    )

    assert asyncio.run(feature._send_nudge(channel)) is False
    channel.send.assert_not_awaited()


def test_failed_generation_keeps_guild_overdue_for_retry(monkeypatch):
    feature = object.__new__(InactivityFeature)
    feature.client = MagicMock()
    channel = MagicMock()
    feature.client.get_channel.return_value = channel
    feature._send_nudge = AsyncMock(return_value=False)
    feature._state = {7: {"last_time": 100.0, "channel_id": 99}}
    save_activity = MagicMock()

    monkeypatch.setattr("features.inactivity.time.time", lambda: 100_000.0)
    monkeypatch.setattr("features.inactivity.db.set_guild_activity", save_activity)

    asyncio.run(InactivityFeature._check.coro(feature))

    assert feature._state[7]["last_time"] == 100.0
    save_activity.assert_not_called()


def test_capacity_skip_keeps_guild_overdue_for_retry(monkeypatch):
    feature = object.__new__(InactivityFeature)
    feature.client = MagicMock()
    channel = MagicMock()
    channel.send = AsyncMock()
    feature.client.get_channel.return_value = channel
    feature._pick_recent_chatter = AsyncMock(return_value=None)
    feature._state = {7: {"last_time": 100.0, "channel_id": 99}}
    save_activity = MagicMock()
    post = MagicMock(side_effect=AssertionError("busy nudge must not contact server"))
    monkeypatch.setattr("llm.client.requests.post", post)
    monkeypatch.setattr("features.inactivity.resolve_bot_display_name", lambda *args: "Bot")
    monkeypatch.setattr("features.inactivity.time.time", lambda: 100_000.0)
    monkeypatch.setattr("features.inactivity.db.is_guild_inactivity_enabled", lambda _: True)
    monkeypatch.setattr("features.inactivity.db.set_guild_activity", save_activity)
    reservation = reserve()
    try:
        asyncio.run(InactivityFeature._check.coro(feature))
        assert feature._state[7]["last_time"] == 100.0
        save_activity.assert_not_called()
        channel.send.assert_not_awaited()
        post.assert_not_called()
    finally:
        reservation.release()


def test_disabled_guild_is_skipped(monkeypatch):
    feature = object.__new__(InactivityFeature)
    feature.client = MagicMock()
    feature._send_nudge = AsyncMock(return_value=True)
    feature._state = {7: {"last_time": 100.0, "channel_id": 99}}

    monkeypatch.setattr("features.inactivity.time.time", lambda: 100_000.0)
    monkeypatch.setattr(
        "features.inactivity.db.is_guild_inactivity_enabled", lambda guild_id: False
    )

    asyncio.run(InactivityFeature._check.coro(feature))

    feature.client.get_channel.assert_not_called()
    feature._send_nudge.assert_not_awaited()


def test_llm_inactivity_command_updates_current_guild(tmp_db, monkeypatch):
    monkeypatch.setattr("features.inactivity.db.get_all_guild_activity", lambda: [])
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    InactivityFeature(client, tree)
    interaction = MagicMock()
    interaction.guild_id = 77
    interaction.user.guild_permissions.manage_guild = True
    interaction.user.guild_permissions.administrator = False
    interaction.response.send_message = AsyncMock()

    asyncio.run(
        tree.get_command("llm-inactivity").callback(interaction, "deactivate")
    )

    assert db.is_guild_inactivity_enabled(77) is False
    interaction.response.send_message.assert_awaited_once_with(
        "LLM inactivity nudges are now **deactivated** for this server.",
        ephemeral=True,
    )
    asyncio.run(client.close())


def _activity_message(guild_id, channel_id):
    message = MagicMock()
    message.guild.id = guild_id
    message.channel.id = channel_id
    return message


def test_activity_writes_are_debounced(monkeypatch):
    feature = object.__new__(InactivityFeature)
    feature._state = {}
    save_activity = MagicMock()
    now = [1000.0]
    monkeypatch.setattr("features.inactivity.time.time", lambda: now[0])
    monkeypatch.setattr("features.inactivity.db.set_guild_activity", save_activity)

    for _ in range(10):
        asyncio.run(feature.handle_message(_activity_message(7, 99)))
        now[0] += 5

    save_activity.assert_called_once_with(7, 1000.0, 99)
    assert feature._state[7]["last_time"] == 1045.0
    assert feature._state[7]["channel_id"] == 99


def test_activity_persists_on_channel_change_and_after_interval(monkeypatch):
    feature = object.__new__(InactivityFeature)
    feature._state = {7: {"last_time": 900.0, "channel_id": 99, "persisted_time": 990.0}}
    save_activity = MagicMock()
    now = [1000.0]
    monkeypatch.setattr("features.inactivity.time.time", lambda: now[0])
    monkeypatch.setattr("features.inactivity.db.set_guild_activity", save_activity)

    asyncio.run(feature.handle_message(_activity_message(7, 99)))
    save_activity.assert_not_called()

    asyncio.run(feature.handle_message(_activity_message(7, 55)))
    save_activity.assert_called_once_with(7, 1000.0, 55)

    now[0] = 1059.0
    asyncio.run(feature.handle_message(_activity_message(7, 55)))
    assert save_activity.call_count == 1

    now[0] = 1060.0
    asyncio.run(feature.handle_message(_activity_message(7, 55)))
    assert save_activity.call_count == 2
    save_activity.assert_called_with(7, 1060.0, 55)
