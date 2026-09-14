import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
from discord import app_commands

import db
from features.inactivity import InactivityFeature, pick_chatter


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
        tree.get_command("llm_inactivity").callback(interaction, "deactivate")
    )

    assert db.is_guild_inactivity_enabled(77) is False
    interaction.response.send_message.assert_awaited_once_with(
        "LLM inactivity nudges are now **deactivated** for this server.",
        ephemeral=True,
    )
    asyncio.run(client.close())
