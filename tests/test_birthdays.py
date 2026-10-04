import asyncio
import sqlite3
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
from discord import app_commands

import db
from features import birthdays
from features.birthdays import (
    BirthdaysFeature,
    _next_birthday_check,
    _parse_birthday,
)
from llm.capacity import reserve


def _interaction(*, user_id=1, channel_id=10, guild_id=20):
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.channel_id = channel_id
    interaction.guild_id = guild_id
    interaction.response.send_message = AsyncMock()
    return interaction


def _command_feature():
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    feature = BirthdaysFeature(client, tree)
    return client, tree, feature


_OMITTED = object()


def _invoke(tree, interaction, value=_OMITTED):
    callback = tree.get_command("set-birthday").callback
    if value is _OMITTED:
        return asyncio.run(callback(interaction))
    return asyncio.run(callback(interaction, value))


def test_parser_accepts_supported_formats_and_surrounding_whitespace():
    assert _parse_birthday("25.12") == (12, 25)
    assert _parse_birthday(" 1-2 ") == (2, 1)
    assert _parse_birthday("29/02") == (2, 29)


def test_parser_rejects_years_natural_language_blank_and_impossible_dates():
    for value in ("25.12.2000", "December 25", "", "   ", "31.04", "29.02.1900"):
        assert _parse_birthday(value) is None


def test_birthday_checks_are_anchored_to_local_0001_with_30_minute_retries():
    assert _next_birthday_check(datetime(2026, 12, 25, 0, 0, 0)) == datetime(
        2026, 12, 25, 0, 1, 0
    )
    assert _next_birthday_check(datetime(2026, 12, 25, 0, 1, 30)) == datetime(
        2026, 12, 25, 0, 1, 30
    )
    assert _next_birthday_check(datetime(2026, 12, 25, 0, 2, 0)) == datetime(
        2026, 12, 25, 0, 31, 0
    )
    assert _next_birthday_check(datetime(2026, 12, 25, 14, 45, 0)) == datetime(
        2026, 12, 25, 15, 1, 0
    )


def test_command_signature_is_optional_string():
    client, tree, _feature = _command_feature()
    command = tree.get_command("set-birthday")
    assert command is not None
    assert command.name == "set-birthday"
    assert command.parameters[0].name == "date"
    assert command.parameters[0].required is False
    assert command.parameters[0].type.name == "string"
    asyncio.run(client.close())


def test_command_save_replace_delete_and_private_confirmation(tmp_db):
    client, tree, _feature = _command_feature()
    interaction = _interaction()
    _invoke(tree, interaction, "25.12")
    first = db.get_birthday(1)
    assert first[:4] == (10, 20, 12, 25)
    interaction.response.send_message.assert_awaited_once()
    assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True
    assert "25.12" in interaction.response.send_message.call_args.args[0]

    replacement = _interaction(channel_id=30, guild_id=40)
    _invoke(tree, replacement, "1/2")
    assert db.get_birthday(1)[:4] == (30, 40, 2, 1)

    deletion = _interaction(channel_id=None, guild_id=None)
    _invoke(tree, deletion)
    assert db.get_birthday(1) is None
    assert deletion.response.send_message.call_args.kwargs["ephemeral"] is True
    asyncio.run(client.close())


def test_command_invalid_and_blank_preserve_previous_registration(tmp_db):
    client, tree, _feature = _command_feature()
    original = _interaction()
    _invoke(tree, original, "25.12")
    before = db.get_birthday(1)

    for value in ("", "31.04", "December 25"):
        interaction = _interaction()
        _invoke(tree, interaction, value)
        assert db.get_birthday(1) == before
        assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True
    asyncio.run(client.close())


def test_command_saves_dm_destination_and_missing_channel_is_private(tmp_db):
    client, tree, _feature = _command_feature()
    dm = _interaction(channel_id=99, guild_id=None)
    _invoke(tree, dm, "2-3")
    assert db.get_birthday(1)[:4] == (99, None, 3, 2)

    missing = _interaction(channel_id=None, guild_id=20)
    _invoke(tree, missing, "3.4")
    assert db.get_birthday(1)[:4] == (99, None, 3, 2)
    assert missing.response.send_message.call_args.kwargs["ephemeral"] is True
    asyncio.run(client.close())


def test_command_db_error_is_private_and_does_not_generate(tmp_db, monkeypatch):
    client, tree, _feature = _command_feature()
    record = AsyncMock()
    generation = MagicMock(side_effect=AssertionError("command must not generate"))
    monkeypatch.setattr(birthdays, "record_for", record)
    monkeypatch.setattr(birthdays.db, "set_birthday", MagicMock(side_effect=sqlite3.OperationalError("locked")))
    monkeypatch.setattr(birthdays, "generate_birthday_message", generation)

    interaction = _interaction()
    _invoke(tree, interaction, "25.12")
    assert "try again" in interaction.response.send_message.call_args.args[0]
    assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True
    record.assert_awaited_once_with("failure", "command/set-birthday", interaction)
    generation.assert_not_called()
    asyncio.run(client.close())


def _delivery_feature(channel=None, *, fetch=None):
    client = SimpleNamespace(
        get_channel=MagicMock(return_value=channel),
        fetch_channel=AsyncMock(return_value=fetch),
        user=SimpleNamespace(id=999),
    )
    feature = BirthdaysFeature.__new__(BirthdaysFeature)
    feature.client = client
    return feature


def _channel(guild_id=20):
    channel = SimpleNamespace(
        guild=SimpleNamespace(id=guild_id) if guild_id is not None else None,
        send=AsyncMock(),
    )
    return channel


def test_delivery_sends_once_with_local_date_mentions_and_marker(tmp_db, monkeypatch):
    db.set_birthday(1, 10, 20, 12, 25)
    channel = _channel()
    feature = _delivery_feature(channel)
    records = []

    async def fake_record(category, activity, **kwargs):
        records.append((category, activity, kwargs))

    monkeypatch.setattr(birthdays, "record", fake_record)
    monkeypatch.setattr(birthdays, "generate_birthday_message", lambda: "Have fun!")
    asyncio.run(
        feature._send_birthday(
            db.get_due_birthdays(date(2026, 12, 25))[0], date(2026, 12, 25)
        )
    )

    channel.send.assert_awaited_once()
    assert channel.send.call_args.args[0] == "🎂 <@1> Have fun!"
    mentions = channel.send.call_args.kwargs["allowed_mentions"]
    assert [user.id for user in mentions.users] == [1]
    assert mentions.roles is False
    assert mentions.everyone is False
    assert mentions.replied_user is False
    assert db.get_birthday(1)[5] == 2026
    assert records == [("scheduled", "birthday-delivery", {"guild_id": 20})]


def test_delivery_fetches_uncached_dm_and_missing_or_forbidden_retries(tmp_db, monkeypatch):
    db.set_birthday(1, 10, None, 12, 25)
    fetched = _channel(None)
    feature = _delivery_feature(None, fetch=fetched)
    generation = MagicMock(return_value="Hello!")
    records = []

    async def fake_record(category, activity, **kwargs):
        records.append((category, activity, kwargs))

    monkeypatch.setattr(birthdays, "record", fake_record)
    monkeypatch.setattr(birthdays, "generate_birthday_message", generation)
    row = db.get_due_birthdays(date(2026, 12, 25))[0]
    asyncio.run(feature._send_birthday(row, date(2026, 12, 25)))
    feature.client.fetch_channel.assert_awaited_once_with(10)
    generation.assert_called_once()
    assert fetched.send.await_count == 1

    db.set_birthday(2, 11, 99, 12, 25)
    missing = _delivery_feature(None, fetch=None)
    asyncio.run(missing._send_birthday(db.get_due_birthdays(date(2026, 12, 25))[-1], date(2026, 12, 25)))
    assert db.get_birthday(2)[5] is None
    assert ("failure", "birthday-delivery", {"guild_id": 99}) in records


def test_delivery_failure_and_generation_empty_leave_due_for_retry(tmp_db, monkeypatch):
    db.set_birthday(1, 10, 20, 12, 25)
    channel = _channel()
    channel.send.side_effect = RuntimeError("send failed")
    feature = _delivery_feature(channel)
    records = []

    async def fake_record(category, activity, **kwargs):
        records.append((category, activity, kwargs))

    monkeypatch.setattr(birthdays, "record", fake_record)
    monkeypatch.setattr(birthdays, "generate_birthday_message", lambda: None)
    row = db.get_due_birthdays(date(2026, 12, 25))[0]
    asyncio.run(feature._send_birthday(row, date(2026, 12, 25)))
    assert channel.send.await_count == 0
    assert db.get_birthday(1)[5] is None
    assert records == [("failure", "birthday-delivery", {"guild_id": 20})]


def test_capacity_skip_leaves_birthday_due_for_retry(tmp_db, monkeypatch):
    db.set_birthday(1, 10, 20, 12, 25)
    channel = _channel()
    feature = _delivery_feature(channel)
    post = MagicMock(side_effect=AssertionError("busy birthday must not contact server"))
    monkeypatch.setattr("llm.client.requests.post", post)
    reservation = reserve()
    try:
        row = db.get_due_birthdays(date(2026, 12, 25))[0]
        asyncio.run(feature._send_birthday(row, date(2026, 12, 25)))
        channel.send.assert_not_awaited()
        post.assert_not_called()
        assert db.get_birthday(1)[5] is None
        assert len(db.get_due_birthdays(date(2026, 12, 25))) == 1
    finally:
        reservation.release()


def test_successful_delivery_is_not_due_for_later_retry(tmp_db, monkeypatch):
    db.set_birthday(1, 10, 20, 12, 25)
    channel = _channel()
    feature = _delivery_feature(channel)

    async def fake_record(*args, **kwargs):
        return None

    monkeypatch.setattr(birthdays, "record", fake_record)
    monkeypatch.setattr(birthdays, "generate_birthday_message", lambda: "Have fun!")
    row = db.get_due_birthdays(date(2026, 12, 25))[0]
    asyncio.run(feature._send_birthday(row, date(2026, 12, 25)))

    assert db.get_due_birthdays(date(2026, 12, 25)) == []
    channel.send.assert_awaited_once()


def test_generation_race_revision_change_suppresses_send(tmp_db, monkeypatch):
    db.set_birthday(1, 10, 20, 12, 25)
    channel = _channel()
    feature = _delivery_feature(channel)
    original = db.get_birthday(1)

    async def fake_to_thread(function, *args, **kwargs):
        db.set_birthday(1, 11, 21, 12, 25)
        return function(*args, **kwargs)

    monkeypatch.setattr(birthdays.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(birthdays, "generate_birthday_message", lambda: "Hello!")
    asyncio.run(feature._send_birthday(
        db.get_due_birthdays(date(2026, 12, 25))[0], date(2026, 12, 25)
    ))
    assert original[4] != db.get_birthday(1)[4]
    channel.send.assert_not_awaited()


def test_state_write_failure_still_counts_accepted_send(tmp_db, monkeypatch):
    db.set_birthday(1, 10, 20, 12, 25)
    channel = _channel()
    feature = _delivery_feature(channel)
    records = []

    async def fake_record(category, activity, **kwargs):
        records.append((category, activity, kwargs))

    monkeypatch.setattr(birthdays, "record", fake_record)
    monkeypatch.setattr(birthdays, "generate_birthday_message", lambda: "Hello!")
    monkeypatch.setattr(birthdays.db, "mark_birthday_sent", MagicMock(side_effect=sqlite3.OperationalError("locked")))
    asyncio.run(feature._send_birthday(
        db.get_due_birthdays(date(2026, 12, 25))[0], date(2026, 12, 25)
    ))
    assert records == [
        ("failure", "birthday-state", {"guild_id": 20}),
        ("scheduled", "birthday-delivery", {"guild_id": 20}),
    ]


def test_start_tasks_starts_only_once(monkeypatch):
    feature = BirthdaysFeature.__new__(BirthdaysFeature)
    loop = MagicMock()
    loop.is_running.side_effect = [False, True]
    feature._check = loop
    asyncio.run(feature.start_tasks())
    asyncio.run(feature.start_tasks())
    loop.start.assert_called_once_with()
