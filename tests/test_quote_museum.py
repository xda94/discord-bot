import asyncio
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from db import community
from features import quote_museum as museum
from features.social_games import SocialGamesFeature


def interaction(user_id=42, guild_id=12, language="en", manage_messages=False, message_id=99, channel_id=23):
    guild = SimpleNamespace(id=guild_id, name="Secret Society") if guild_id else None
    user = SimpleNamespace(id=user_id, display_name="Robeeque", bot=False, guild=guild, guild_permissions=SimpleNamespace(manage_messages=manage_messages), send=AsyncMock())
    message = SimpleNamespace(id=message_id, edit=AsyncMock())
    channel = SimpleNamespace(id=channel_id, name="secret-quotes", guild=guild, send=AsyncMock(return_value=message), fetch_message=AsyncMock(return_value=message))
    response = SimpleNamespace(is_done=MagicMock(return_value=False), defer=AsyncMock())
    response.defer.side_effect = lambda **kwargs: setattr(response.is_done, "return_value", True)
    return SimpleNamespace(id=90, user=user, guild=guild, guild_id=guild_id, channel=channel, channel_id=channel_id, message=message, response=response, followup=SimpleNamespace(send=AsyncMock()), locale=language)


def submission(value, content="  Biscuits **escaped** @everyone\n ", author_id=45, language="en"):
    sent = SimpleNamespace(id=101, channel=SimpleNamespace(id=102), delete=AsyncMock())
    author = SimpleNamespace(id=author_id, guild=value.guild, bot=False, system=False, display_name="Ada", name="Ada", locale=language, send=AsyncMock(return_value=sent))
    return SimpleNamespace(author=author, content=content)


def feature(value=None, games=None):
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    client.add_view = MagicMock()
    guild = SimpleNamespace(id=12, fetch_member=AsyncMock(return_value=SimpleNamespace(id=45, bot=False)))
    client.get_guild = MagicMock(return_value=guild)
    client._connection._guilds = {12: guild}
    if value:
        client.get_channel = MagicMock(return_value=value.channel)
        client.fetch_channel = AsyncMock(return_value=value.channel)
    return museum.QuoteMuseumFeature(client, tree, games or SimpleNamespace(create_quiz=AsyncMock(return_value="quiz")))


def quote(author=45, message_id=90, approved=False, guild_id=12, now=None, name="Ada"):
    row = community.create_quote_request(guild_id, 67, message_id, 42, author, "  Biscuit\n ", name, "en", now=now)
    community.bind_quote_consent_message(guild_id, row["id"], 101, 102)
    if approved:
        community.approve_quote(guild_id, row["id"], author, now=now)
    return community.get_quote(guild_id, row["id"], now=now)


def control(author=45, **kwargs):
    return interaction(user_id=author, guild_id=None, message_id=101, channel_id=102, **kwargs)


@pytest.fixture(autouse=True)
def external_mocks(monkeypatch):
    monkeypatch.setattr(museum, "record", AsyncMock())


def test_registration_constructor_and_option_names():
    worker = feature()
    commands = worker.tree.get_commands()
    assert {item.name for item in commands} == {"quote-add", "quote", "who-said", "quote-delete"}
    assert all(item.guild_only for item in commands)
    assert all(isinstance(item, app_commands.Command) for item in commands)
    assert [parameter.display_name for parameter in worker.tree.get_command("quote-add").parameters] == ["quote", "user"]
    assert worker.tree.get_command("quote-delete").parameters[0].display_name == "id"
    assert worker._startup_lock is None


@pytest.mark.parametrize("language", ["en", "ro"])
def test_private_nomination_exact_snapshot_disclosure_and_language(tmp_db, language):
    value = interaction(language=language)
    message = submission(value, language=language)
    row = asyncio.run(feature().nominate(value, message.content, message.author))
    assert row["quote_text"] == message.content
    assert row["status"] == "pending" and row["language"] == language
    assert row["consent_expires_at"] == row["created_at"] + 86400
    assert row["source_channel_id"] == value.channel_id and row["source_message_id"] == value.id and row["consent_message_id"] == 101
    assert community.get_quote(12, row["id"], approved_only=True) is None
    assert community.choose_quote(12) is None
    message.author.send.assert_awaited_once()
    call = message.author.send.call_args
    card = call.kwargs["embed"]
    assert card.title == museum._t("consent_title", language, id=row["id"])
    quoted = "\n".join("> " + line for line in museum.safe_text(message.content, 3000).split("\n"))
    assert card.description == f"**{museum._t('quote_label', language)}**\n{quoted}"
    fields = {field.name: field.value for field in card.fields}
    assert fields[museum._t("server_label", language)] == "Secret Society"
    assert fields[museum._t("channel_label", language)] == "#secret-quotes"
    assert fields[museum._t("nominator_label", language)] == "Robeeque · <@42>"
    disclosure = fields[museum._t("consent_label", language)]
    assert "24" in disclosure and ("cannot see" if language == "en" else "nu pot vedea") in disclosure
    assert ("screenshots" if language == "en" else "capturi de ecran") in disclosure
    assert [button.custom_id for button in call.kwargs["view"].children] == [f"museum:{row['id']}:accept", f"museum:{row['id']}:decline"]
    assert call.kwargs["allowed_mentions"].everyone is False and call.kwargs["allowed_mentions"].users is False
    assert call.kwargs["suppress_embeds"] is False
    value.channel.send.assert_not_called()
    assert value.followup.send.call_args.kwargs["ephemeral"] is True
    museum.record.assert_not_called()


@pytest.mark.parametrize("content", ["  " + "*" * 1196 + "\n ", "Q" + "\n" * 1199])
def test_full_1200_character_snapshot_and_saved_author_language(tmp_db, monkeypatch, content):
    import db

    value = interaction(language="ro")
    message = submission(value, content=content, language="ro")
    monkeypatch.setattr(db, "get_assistant_profile", lambda user_id: {"language": "en"} if user_id == 45 else None)
    row = asyncio.run(feature().nominate(value, message.content, message.author))
    assert row["language"] == "en"
    assert row["quote_text"] == content and len(row["quote_text"]) == 1200
    card = message.author.send.call_args.kwargs["embed"]
    quoted = "\n".join("> " + line for line in museum.safe_text(content, 3000).split("\n"))
    assert card.description == f"**Quote attributed to you**\n{quoted}"
    assert len(card.description) <= 4096 and len(card) <= 6000
    assert all(len(field.value) <= 1024 for field in card.fields)
    assert "throughout that server" in card.fields[-1].value
    assert value.followup.send.call_args.args[0] == museum._t("nominated", "ro")


@pytest.mark.parametrize("invalid", ["bot", "system", "guild", "channel", "empty", "missing", "large"])
def test_nomination_validation(tmp_db, invalid):
    value = interaction()
    message = submission(value)
    if invalid in ("bot", "system"):
        setattr(message.author, invalid, True)
    elif invalid == "guild":
        message.author.guild = SimpleNamespace(id=13)
    elif invalid == "channel":
        value.channel.guild = SimpleNamespace(id=13)
    else:
        message.content = {"empty": "\n ", "missing": None, "large": "a" * 1201}[invalid]
    assert asyncio.run(feature().nominate(value, message.content, message.author)) is None
    assert community.list_pending_quotes() == []
    message.author.send.assert_not_called()
    value.channel.send.assert_not_called()


@pytest.mark.parametrize("method", ["nominate", "quote", "quiz", "delete"])
def test_guild_only_and_human_admission(tmp_db, method):
    worker = feature()
    for guild_id, bot in [(None, False), (12, True)]:
        value = interaction(guild_id=guild_id)
        value.user.bot = bot
        args = (value, 1) if method == "delete" else (value,)
        if method == "nominate":
            proposed = submission(value)
            args = (value, proposed.content, proposed.author)
        assert asyncio.run(getattr(worker, method)(*args)) is None
        value.channel.send.assert_not_called()


def test_blocked_dm_erases_pending_and_leaks_nothing(tmp_db):
    value = interaction()
    message = submission(value, content="private reason")
    message.author.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "blocked")
    assert asyncio.run(feature().nominate(value, message.content, message.author)) is None
    assert community.list_pending_quotes() == []
    value.channel.send.assert_not_called()
    assert "private reason" not in value.followup.send.call_args.args[0]
    assert value.followup.send.call_args.args[0] == museum.text("dm_failed", "en")
    assert museum.record.call_args.args[:2] == ("failure", "museum/nominate")


def test_consent_card_escapes_quote_and_source_names(tmp_db):
    value = interaction()
    value.guild.name = "**Server** @everyone"
    value.channel.name = "quotes_@here"
    value.user.display_name = "**Admin** @everyone"
    message = submission(value, content="First line\n> **Second line** @everyone <@&123>")
    assert asyncio.run(feature().nominate(value, message.content, message.author))
    card = message.author.send.call_args.kwargs["embed"]
    assert "\n> \\> \\*\\*Second line\\*\\*" in card.description
    assert "@everyone" not in card.description
    assert card.fields[0].value == museum.safe_text(value.guild.name, 200)
    assert card.fields[1].value == "#" + museum.safe_text(value.channel.name, 200)
    assert card.fields[2].value == museum.safe_text(value.user.display_name, 200) + " · <@42>"


def test_bind_failure_erases_nomination_and_messages(tmp_db, monkeypatch):
    value = interaction()
    message = submission(value)
    monkeypatch.setattr(community, "bind_quote_consent_message", lambda *args: {"status": "missing", "quote": None})
    assert asyncio.run(feature().nominate(value, message.content, message.author)) is None
    assert community.list_pending_quotes() == []
    message.author.send.return_value.delete.assert_awaited_once()


def test_duplicate_does_not_send_another_dm(tmp_db):
    row = quote()
    value = interaction()
    message = submission(value)
    assert asyncio.run(feature().nominate(value, message.content, message.author)) is None
    message.author.send.assert_not_called()
    assert community.get_quote(12, row["id"])["status"] == "pending"


def test_quote_add_callback_accepts_text_and_member_for_separate_submissions(tmp_db):
    value = interaction()
    proposed = submission(value)
    worker = feature()

    async def run():
        command = worker.tree.get_command("quote-add")
        await command.callback(value, proposed.content, proposed.author)
        value.id += 1
        await command.callback(value, "Another quote", proposed.author)

    asyncio.run(run())
    pending = community.list_pending_quotes()
    assert len(pending) == 2
    assert {row["source_message_id"] for row in pending} == {90, 91}
    assert {row["quote_text"] for row in pending} == {proposed.content, "Another quote"}
    assert all(row["author_id"] == proposed.author.id for row in pending)
    assert proposed.author.send.await_count == 2
    value.channel.send.assert_not_called()


@pytest.mark.parametrize("invalid", ["wrong_user", "bot", "guild", "message", "channel", "record_guild"])
def test_consent_author_dm_and_record_scope(tmp_db, invalid):
    row = quote()
    value = control()
    guild_id = 12
    if invalid == "wrong_user":
        value.user.id = 88
    elif invalid == "bot":
        value.user.bot = True
    elif invalid == "guild":
        value.guild_id = 12
    elif invalid == "message":
        value.message.id = 105
    elif invalid == "channel":
        value.channel_id = 105
    elif invalid == "record_guild":
        guild_id = 13
    asyncio.run(feature().handle_control(value, guild_id, row["id"], "accept"))
    assert community.get_quote(12, row["id"])["status"] == "pending"
    value.message.edit.assert_not_called()


def test_approval_checks_membership_and_replaces_persistent_view(tmp_db):
    row = quote()
    value = control(language="ro")
    worker = feature()
    asyncio.run(worker.handle_control(value, 12, row["id"], "accept"))
    changed = community.get_quote(12, row["id"], approved_only=True)
    assert changed["quote_text"] == "  Biscuit\n "
    worker.client.get_guild.return_value.fetch_member.assert_awaited_once_with(45)
    view = value.message.edit.call_args.kwargs["view"]
    assert view.timeout is None and view.is_persistent()
    assert [button.custom_id for button in view.children] == [f"museum:{row['id']}:remove"]
    assert "aprobat" in value.followup.send.call_args.args[0]
    assert "approved" in value.message.edit.call_args.kwargs["content"]
    assert value.message.edit.call_args.kwargs["embed"] is None
    assert museum.record.call_args.args[:2] == ("control", "museum/accept")


def test_left_guild_cannot_approve(tmp_db):
    row = quote()
    value = control()
    worker = feature()
    worker.client.get_guild.return_value.fetch_member.side_effect = discord.NotFound(SimpleNamespace(status=404, reason="Not found"), "gone")
    asyncio.run(worker.handle_control(value, 12, row["id"], "accept"))
    assert community.get_quote(12, row["id"])["status"] == "pending"
    assert value.followup.send.call_args.args[0] == museum._t("membership", "en")


def test_membership_fetch_failure_is_private_and_keeps_pending(tmp_db):
    row = quote()
    worker = feature()
    worker.client.get_guild.return_value.fetch_member.side_effect = RuntimeError("content")
    value = control()
    asyncio.run(worker.handle_control(value, 12, row["id"], "accept"))
    assert community.get_quote(12, row["id"])["status"] == "pending"
    assert value.followup.send.call_args.args[0] == museum.text("failed", "en")


def test_atomic_deadline_checked_after_member_fetch(tmp_db, monkeypatch):
    row = quote()
    worker = feature()

    async def fetch_member(author_id):
        monkeypatch.setattr(community.time, "time", lambda: row["consent_expires_at"])
        return SimpleNamespace(id=author_id, bot=False)

    worker.client.get_guild.return_value.fetch_member.side_effect = fetch_member
    value = control()
    asyncio.run(worker.handle_control(value, 12, row["id"], "accept"))
    assert community.get_quote(12, row["id"]) is None
    assert value.followup.send.call_args.args[0] == museum.text("expired", "en")


def test_decline_erases_snapshot(tmp_db):
    row = quote()
    value = control()
    asyncio.run(feature().handle_control(value, 12, row["id"], "decline"))
    assert community.get_quote(12, row["id"]) is None
    assert all(button.disabled for button in value.message.edit.call_args.kwargs["view"].children)
    assert value.message.edit.call_args.kwargs["embed"] is None
    assert museum.record.call_args.args[:2] == ("control", "museum/decline")


@pytest.mark.parametrize("action,approved", [("remove", False), ("accept", True), ("decline", True)])
def test_stale_controls_recheck_status(tmp_db, action, approved):
    row = quote(approved=approved)
    value = control()
    asyncio.run(feature().handle_control(value, 12, row["id"], action))
    assert community.get_quote(12, row["id"])["status"] == ("approved" if approved else "pending")
    assert value.followup.send.call_args.args[0] == museum.text("expired", "en")
    museum.record.assert_not_called()


def test_author_withdrawal_after_leaving_guild_revokes_quiz(tmp_db):
    row = quote(approved=True)
    payload = {"quote_id": row["id"], "quote_text": row["quote_text"], "correct_author_id": 45, "candidates": [{"user_id": 45, "name": "Ada"}, {"user_id": 46, "name": "Bob"}]}
    session = community.create_session(12, 23, 42, "quiz", "en", payload)
    community.bind_session_message(12, session["id"], 99)
    worker = feature()
    worker.client.get_guild.return_value.fetch_member.side_effect = AssertionError("withdrawal must not require membership")
    value = control()
    asyncio.run(worker.handle_control(value, 12, row["id"], "remove"))
    assert community.get_quote(12, row["id"]) is None
    changed = community.get_session(12, session["id"])
    assert changed["phase"] == "cancelled" and changed["payload"] == {} and changed["result_text"] is None
    assert "screenshots" in value.followup.send.call_args.args[0]
    assert museum.record.call_args.args[:2] == ("control", "museum/remove")


def test_failed_approval_edit_delivers_removal_dm(tmp_db):
    row = quote()
    value = control()
    value.message.edit.side_effect = RuntimeError("edit")
    value.user.send.return_value = SimpleNamespace(id=200, channel=SimpleNamespace(id=201))
    asyncio.run(feature().handle_control(value, 12, row["id"], "accept"))
    value.user.send.assert_awaited_once()
    assert value.user.send.call_args.kwargs["view"].children[0].custom_id.endswith(":remove")
    assert community.get_quote(12, row["id"])["consent_message_id"] == 200


@pytest.mark.parametrize("author,moderator,allowed", [(45, False, True), (42, True, True), (42, False, False)])
def test_slash_deletion_author_and_moderator_permissions(tmp_db, author, moderator, allowed):
    row = quote(approved=True)
    value = interaction(user_id=author, manage_messages=moderator)
    asyncio.run(feature().delete(value, row["id"]))
    assert (community.get_quote(12, row["id"]) is None) == allowed
    value.channel.send.assert_not_called()


def test_wrong_guild_deletion_cannot_remove_quote(tmp_db):
    row = quote(approved=True)
    value = interaction(user_id=45, guild_id=13, manage_messages=True)
    asyncio.run(feature().delete(value, row["id"]))
    assert community.get_quote(12, row["id"], approved_only=True)


def test_approved_guild_reads_only_and_safe_output(tmp_db):
    quote()
    value = interaction()
    worker = feature()
    assert asyncio.run(worker.quote(value)) is None
    value.channel.send.assert_not_called()
    row = quote(approved=True, message_id=91)
    assert asyncio.run(worker.quote(value))["id"] == row["id"]
    sent = value.channel.send.call_args
    assert "Ada" in sent.args[0] and f"#{row['id']}" in sent.args[0] and row["quote_text"] in sent.args[0]
    assert sent.kwargs["allowed_mentions"].everyone is False and sent.kwargs["suppress_embeds"] is True
    other = interaction(guild_id=13)
    assert asyncio.run(worker.quote(other)) is None


def test_quiz_pool_requires_distinct_approved_authors(tmp_db):
    quote(approved=True)
    quote(approved=True, message_id=91)
    quote(author=46, message_id=92)
    value = interaction()
    worker = feature()
    assert asyncio.run(worker.quiz(value)) is None
    worker.games.create_quiz.assert_not_called()
    assert value.followup.send.call_args.args[0] == museum._t("pool", "en")


def test_quiz_distinct_candidates_include_chosen_author_beyond_latest_100(tmp_db, monkeypatch):
    chosen = quote(author=99, approved=True, name="Zoe")
    for index in range(110):
        quote(author=45 + index % 4, approved=True, message_id=1000 + index, name=f"Author {index % 4}")
    monkeypatch.setattr(community, "choose_quote", lambda guild_id: chosen)
    worker = feature()
    value = interaction()
    assert asyncio.run(worker.quiz(value)) == "quiz"
    args = worker.games.create_quiz.call_args.args
    candidates = args[2]
    assert 2 <= len(candidates) <= 4
    assert len({item["user_id"] for item in candidates}) == len(candidates)
    assert 99 in {item["user_id"] for item in candidates}
    assert all(item["name"].strip() for item in candidates)
    value.channel.send.assert_not_called()


def test_quiz_pool_paginates_to_find_second_author(tmp_db):
    for index in range(101):
        quote(approved=True, message_id=1000 + index)
    quote(author=46, approved=True, message_id=2000, name="Bob")
    worker = feature()
    value = interaction()
    assert asyncio.run(worker.quiz(value)) == "quiz"
    assert {item["user_id"] for item in worker.games.create_quiz.call_args.args[2]} == {45, 46}


def test_quiz_shared_display_names_remain_distinguishable_in_buttons(tmp_db):
    for index in range(4):
        quote(author=1234567890123456700 + index, approved=True, message_id=1000 + index, name="a" * 100)
    value = interaction()
    client = discord.Client(intents=discord.Intents.none())
    games = SocialGamesFeature(client, app_commands.CommandTree(client))
    row = asyncio.run(feature(value, games).quiz(value))
    sent = value.channel.send.call_args
    labels = [button.label for button in sent.kwargs["view"].children]
    assert len(set(labels)) == 4 and all(len(label) <= 80 for label in labels)
    for index, candidate in enumerate(row["payload"]["candidates"]):
        assert labels[index].startswith(f"{index + 1}. ")
        assert str(candidate["user_id"]) not in candidate["name"] and str(candidate["user_id"]) not in labels[index]
        assert f"<@{candidate['user_id']}>" in sent.args[0]
    assert sent.kwargs["allowed_mentions"].users is False


def test_quiz_different_names_that_truncate_identically_are_distinguished(tmp_db):
    quote(approved=True, name="a" * 80 + "Alice")
    quote(author=46, approved=True, message_id=91, name="a" * 80 + "Bob")
    value = interaction()
    client = discord.Client(intents=discord.Intents.none())
    games = SocialGamesFeature(client, app_commands.CommandTree(client))
    row = asyncio.run(feature(value, games).quiz(value))
    labels = [button.label for button in value.channel.send.call_args.kwargs["view"].children]
    assert len(set(labels)) == 2 and all(len(label) <= 80 for label in labels)
    assert all(str(candidate["user_id"]) not in candidate["name"] for candidate in row["payload"]["candidates"])


@pytest.mark.parametrize("language", ["en", "ro"])
@pytest.mark.parametrize("name", ["123456789012345678", "Ada (123456789012345678)"])
def test_legacy_quote_author_ids_are_replaced_with_readable_names_and_mentions(tmp_db, language, name):
    quote(author=123456789012345678, approved=True, name=name)
    value = interaction(language=language)
    assert asyncio.run(feature().quote(value))
    content = value.channel.send.call_args.args[0]
    assert "<@123456789012345678>" in content
    assert "123456789012345678" not in content.replace("<@123456789012345678>", "")
    assert ("Ada" if name.startswith("Ada") else museum.text("unknown_member", language)) in content
    assert value.channel.send.call_args.kwargs["allowed_mentions"].users is False


@pytest.mark.parametrize("language", ["en", "ro"])
def test_nomination_without_author_name_does_not_store_an_id_label(tmp_db, language):
    value = interaction()
    proposed = submission(value, author_id=123456789012345678, language=language)
    proposed.author.display_name = proposed.author.name = None
    row = asyncio.run(feature().nominate(value, proposed.content, proposed.author))
    assert row["author_name"] == museum.text("unknown_member", language)


def test_real_social_quiz_integration_without_model(tmp_db, monkeypatch):
    from features import social_games

    monkeypatch.setattr(social_games, "generate_humor", AsyncMock(side_effect=AssertionError("no model")))
    monkeypatch.setattr(social_games, "record", AsyncMock())
    quote(approved=True)
    quote(author=46, approved=True, message_id=91, name="Bob")
    value = interaction()
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    games = SocialGamesFeature(client, tree)
    worker = museum.QuoteMuseumFeature(client, tree, games)
    row = asyncio.run(worker.quiz(value))
    assert row["kind"] == "quiz" and row["message_id"] == 99
    assert len(row["payload"]["candidates"]) == 2
    assert value.response.defer.await_count == 1
    social_games.generate_humor.assert_not_called()


def test_expiry_erases_all_requests_despite_per_message_failure(tmp_db):
    first = quote(now=time.time() - 90000)
    second = quote(now=time.time() - 90000, message_id=91)
    value = control()
    value.channel.fetch_message.side_effect = [RuntimeError("one failure"), value.message]
    worker = feature(value)
    asyncio.run(worker._expire_requests())
    assert community.get_quote(12, first["id"]) is None
    assert community.get_quote(12, second["id"]) is None
    value.message.edit.assert_awaited_once()
    assert all(button.disabled for button in value.message.edit.call_args.kwargs["view"].children)
    assert value.message.edit.call_args.kwargs["embed"] is None
    assert museum.record.call_args.args[:2] == ("failure", "museum/expiry")


def test_expiry_loop_database_failure_does_not_escape(tmp_db, monkeypatch):
    monkeypatch.setattr(community, "list_pending_quotes", MagicMock(side_effect=sqlite3.OperationalError("private")))
    worker = feature()
    asyncio.run(worker._expire_requests())
    assert museum.record.call_args.args[:2] == ("failure", "museum/expiry")
    assert "private" not in str(museum.record.call_args_list)


def test_restart_restores_all_pages_pending_and_approved_once(tmp_db):
    for index in range(101):
        quote(message_id=1000 + index, approved=index % 2 == 0)
    for index in range(101):
        quote(message_id=2000 + index, approved=True)
    worker = feature()

    async def run():
        worker._check.start = MagicMock()
        worker._check.is_running = MagicMock(side_effect=[False, True])
        await worker.start_tasks()
        await worker.start_tasks()

    asyncio.run(run())
    assert worker.client.add_view.call_count == 202
    assert worker._check.start.call_count == 1
    views = [call.args[0] for call in worker.client.add_view.call_args_list]
    assert all(view.is_persistent() for view in views)
    assert sum(len(view.children) == 1 for view in views) == 152
    assert sum(len(view.children) == 2 for view in views) == 50


def test_restore_one_bad_view_does_not_skip_remaining_requests(tmp_db):
    quote()
    quote(message_id=91)
    worker = feature()
    worker.client.add_view.side_effect = [RuntimeError("failed"), None]

    async def run():
        worker._check.is_running = MagicMock(return_value=True)
        await worker.start_tasks()

    asyncio.run(run())
    assert worker.client.add_view.call_count == 2
    assert museum.record.call_args.args[:2] == ("failure", "museum/restore")


def test_restart_restores_removal_for_guild_bot_has_left(tmp_db):
    row = quote(approved=True, guild_id=77)
    worker = feature()
    worker.client._connection._guilds = {}

    async def run():
        worker._check.is_running = MagicMock(return_value=True)
        await worker.start_tasks()

    asyncio.run(run())
    worker.client.add_view.assert_called_once()
    assert worker.client.add_view.call_args.kwargs["message_id"] == row["consent_message_id"]
    button = worker.client.add_view.call_args.args[0].children[0]
    assert button.custom_id == f"museum:{row['id']}:remove"
    value = control()
    asyncio.run(button.callback(value))
    assert community.get_quote(77, row["id"]) is None


def test_restart_restores_all_stored_guild_pages(tmp_db):
    for guild_id in range(1000, 1102):
        quote(approved=True, guild_id=guild_id)
    worker = feature()
    worker.client._connection._guilds = {}

    async def run():
        worker._check.is_running = MagicMock(return_value=True)
        await worker.start_tasks()

    asyncio.run(run())
    assert worker.client.add_view.call_count == 102


def test_expired_stale_control_cannot_approve(tmp_db):
    row = quote(now=time.time() - 90000)
    value = control()
    asyncio.run(feature().handle_control(value, 12, row["id"], "accept"))
    assert community.get_quote(12, row["id"]) is None
    value.message.edit.assert_not_called()


def test_deleted_stale_control_cannot_restore_quote(tmp_db):
    row = quote(approved=True)
    community.delete_quote(12, row["id"])
    value = control()
    asyncio.run(feature().handle_control(value, 12, row["id"], "accept"))
    assert community.get_quote(12, row["id"]) is None
    value.message.edit.assert_not_called()
