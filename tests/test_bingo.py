import asyncio
import sqlite3
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

import db
from db import community
from features import bingo


def interaction(user_id=42, guild_id=12, channel_id=23, language="en", manager=False):
    guild = SimpleNamespace(id=guild_id)
    user = SimpleNamespace(id=user_id, bot=False, guild=guild, guild_permissions=SimpleNamespace(manage_guild=manager))
    channel = SimpleNamespace(id=channel_id, guild=guild, send=AsyncMock())
    response = SimpleNamespace(is_done=MagicMock(return_value=False), defer=AsyncMock())
    return SimpleNamespace(user=user, guild=guild, guild_id=guild_id, channel=channel, channel_id=channel_id, response=response, followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock(), locale=language)


def feature():
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    return bingo.BingoFeature(client, tree)


@pytest.fixture(autouse=True)
def external_mocks(monkeypatch):
    monkeypatch.setattr(bingo, "record", AsyncMock())


def test_command_registration_and_optional_parameter():
    worker = feature()
    commands = {command.name: command for command in worker.tree.get_commands()}
    assert set(commands) == {"bingo", "bingo-obicei", "bingo-obicei-sterge"}
    assert all(command.guild_only for command in commands.values())
    payload = commands["bingo-obicei"].to_dict(worker.tree)
    assert [option["name"] for option in payload["options"]] == ["text", "text-en", "id"]
    assert payload["options"][2]["required"] is False
    assert commands["bingo-obicei"].default_permissions.manage_guild
    assert commands["bingo-obicei-sterge"].default_permissions.manage_guild
    assert all(" / " in command.description for command in commands.values())


@pytest.mark.parametrize("language", ["en", "ro"])
def test_seeded_bilingual_board_is_private_and_deterministic(tmp_db, language):
    async def run():
        value = interaction(language=language)
        first = await feature().bingo(value)
        second = await feature().bingo(interaction(user_id=44, language=language))
        assert first["id"] == second["id"] and first["cells"] == second["cells"]
        assert len(first["cells"]) == 9 and len({cell["id"] for cell in first["cells"]}) == 9
        assert all(cell["text_en"] and cell["text_ro"] for cell in community.list_habits(12))
        sent = value.followup.send.call_args
        assert sent.kwargs["ephemeral"] is True
        assert sent.kwargs["allowed_mentions"].everyone is False
        assert sent.kwargs["suppress_embeds"] is True
        embed = sent.kwargs["embed"]
        assert len(embed.fields) == 9 and all(field.inline for field in embed.fields)
        assert first["cells"][0]["text_" + language] in embed.fields[0].value
        assert embed.title == bingo._t("board", language, week=first["week_key"])
        assert "Monday" in embed.description if language == "en" else "luni" in embed.description
        assert "No automatic" in embed.description if language == "en" else "Fără observații" in embed.description
        view = sent.kwargs["view"]
        assert len(view.children[0].options) == 9
        assert all(len(item.custom_id) <= 100 for item in view.children)
        value.channel.send.assert_not_called()
        value.response.defer.assert_awaited_once_with(ephemeral=True)
    asyncio.run(run())


def test_profile_language_overrides_locale(tmp_db):
    db.set_assistant_profile(42, language="ro")
    value = interaction(language="en")
    asyncio.run(feature().bingo(value))
    assert "Bingo săptămânal" in value.followup.send.call_args.kwargs["embed"].title


def test_selector_uses_selected_cell_and_rereads_board(tmp_db, monkeypatch):
    original = community.get_board
    reread = MagicMock(wraps=original)
    monkeypatch.setattr(community, "get_board", reread)

    async def run():
        worker = feature()
        value = interaction()
        board = await worker.bingo(value)
        selector = value.followup.send.call_args.kwargs["view"].children[0]
        selector._values = ["3"]
        await selector.callback(value)
        reread.assert_called_once_with(12, board["id"])
        updated = value.edit_original_response.call_args.kwargs
        assert "Selected cell: 4" in updated["embed"].footer.text
        assert updated["allowed_mentions"].everyone is False
        await updated["view"].children[1].callback(value)
        assert community.get_board(12, board["id"])["marks"][0]["cell_index"] == 3
    asyncio.run(run())


@pytest.mark.parametrize("language", ["en", "ro"])
def test_independent_confirmation_survives_new_feature_instance(tmp_db, language):
    async def run():
        worker = feature()
        owner = interaction(language=language)
        board = await worker.bingo(owner)
        await worker.handle_control(owner, 12, board["id"], 42, 23, "mark", 0)
        await worker.handle_control(owner, 12, board["id"], 42, 23, "confirm", 0)
        assert community.get_board(12, board["id"])["marks"][0]["confirmer_id"] is None
        assert owner.followup.send.call_args.args[0] == bingo._t("self_confirm", language)
        confirmer = interaction(user_id=43, language=language)
        restarted = feature()
        restored = await restarted.bingo(confirmer)
        assert restored["id"] == board["id"]
        await restarted.handle_control(confirmer, 12, board["id"], 43, 23, "confirm", 0)
        mark = community.get_board(12, board["id"])["marks"][0]
        assert mark["marker_id"] == 42 and mark["confirmer_id"] == 43
        assert confirmer.followup.send.call_args.args[0] == bingo._t("confirmation", language)
        third = interaction(user_id=44)
        await restarted.handle_control(third, 12, board["id"], 44, 23, "mark", 0)
        await restarted.handle_control(third, 12, board["id"], 44, 23, "confirm", 0)
        assert community.get_board(12, board["id"])["marks"][0] == mark
        assert [call.args[:2] for call in bingo.record.call_args_list] == [("control", "bingo/mark"), ("control", "bingo/confirm")]
    asyncio.run(run())


def test_unmarked_cell_cannot_be_confirmed(tmp_db):
    board = community.get_or_create_board(12)
    value = interaction()
    asyncio.run(feature().handle_control(value, 12, board["id"], 42, 23, "confirm", 0))
    assert community.get_board(12, board["id"])["marks"] == []
    assert value.followup.send.call_args.args[0] == bingo._t("unmarked", "en")


def test_only_confirmed_cells_complete_rows_columns_diagonals(tmp_db):
    board = community.get_or_create_board(12)
    worker = feature()
    for index in range(9):
        community.mark_bingo_cell(12, board["id"], index, 42)
    pending = worker._render_board(community.get_board(12, board["id"]), "en")
    assert "rows: 0 · columns: 0 · diagonals: 0" in pending.footer.text
    for index in range(9):
        community.confirm_bingo_cell(12, board["id"], index, 43)
    completed = worker._render_board(community.get_board(12, board["id"]), "ro")
    assert "Linii complete: 3 · coloane: 3 · diagonale: 2" in completed.footer.text


@pytest.mark.parametrize("bad", ["bot", "owner", "guild", "channel", "member-guild", "channel-guild"])
def test_controls_reject_wrong_context_and_owner(tmp_db, bad):
    board = community.get_or_create_board(12)
    value = interaction()
    if bad == "bot":
        value.user.bot = True
    elif bad == "owner":
        value.user.id = 43
    elif bad == "guild":
        value = interaction(guild_id=13)
    elif bad == "channel":
        value = interaction(channel_id=24)
    elif bad == "member-guild":
        value.user.guild = SimpleNamespace(id=13)
    else:
        value.channel.guild = SimpleNamespace(id=13)
    asyncio.run(feature().handle_control(value, 12, board["id"], 42, 23, "mark", 0))
    assert community.get_board(12, board["id"])["marks"] == []
    value.edit_original_response.assert_not_called()
    bingo.record.assert_not_called()


@pytest.mark.parametrize("cell,action", [(-1, "mark"), (9, "mark"), (True, "mark"), ("0", "mark"), (0, "other")])
def test_controls_validate_cell_and_action(tmp_db, cell, action):
    board = community.get_or_create_board(12)
    value = interaction(language="ro")
    asyncio.run(feature().handle_control(value, 12, board["id"], 42, 23, action, cell))
    assert community.get_board(12, board["id"])["marks"] == []
    assert value.followup.send.call_args.args[0] == bingo._t("invalid_cell", "ro")


def test_next_week_resets_board_and_rejects_stale_control(tmp_db, monkeypatch):
    board = community.get_or_create_board(12)
    community.mark_bingo_cell(12, board["id"], 0, 42)
    day = bingo.local_day() + timedelta(days=7)
    monkeypatch.setattr(bingo, "local_day", lambda: day)
    original_week = community._week
    monkeypatch.setattr(community, "_week", lambda value=None: original_week(value) if value is not None else bingo.week_key(day))

    async def run():
        worker = feature()
        value = interaction(user_id=43)
        new = await worker.bingo(value)
        assert new["id"] != board["id"] and new["marks"] == []
        assert new["week_key"] == bingo.week_key(day)
        await worker.handle_control(value, 12, board["id"], 43, 23, "confirm", 0)
        assert value.followup.send.call_args.args[0] == bingo.text("expired", "en")
        assert community.get_board(12, board["id"])["marks"][0]["confirmer_id"] is None
    asyncio.run(run())


@pytest.mark.parametrize("method", ["bingo", "save", "delete"])
def test_guild_only_runtime_admission(tmp_db, method):
    value = interaction(guild_id=None)
    worker = feature()
    call = worker.bingo(value) if method == "bingo" else worker.save_habit(value, "RO", "EN") if method == "save" else worker.delete_habit(value, 1)
    asyncio.run(call)
    assert value.followup.send.call_args.args[0] == bingo.text("guild_only", "en")


@pytest.mark.parametrize("language", ["en", "ro"])
@pytest.mark.parametrize("method", ["save", "delete"])
def test_habit_management_requires_manage_guild(tmp_db, language, method):
    habits = community.list_habits(12)
    value = interaction(language=language)
    worker = feature()
    asyncio.run(worker.save_habit(value, "RO", "EN") if method == "save" else worker.delete_habit(value, habits[0]["id"]))
    assert community.list_habits(12) == habits
    assert value.followup.send.call_args.args[0] == bingo._t("permission", language)


@pytest.mark.parametrize("language", ["en", "ro"])
def test_management_lists_ids_and_edits_only_future_boards(tmp_db, language):
    board = community.get_or_create_board(12)
    habit_id = board["cells"][0]["id"]
    value = interaction(language=language, manager=True)

    async def run():
        worker = feature()
        saved = await worker.save_habit(value, "O traducere nouă", "A new translation", habit_id)
        assert saved["status"] == "saved"
        assert f"#{habit_id}:" in value.followup.send.call_args.args[0]
        assert community.get_board(12, board["id"])["cells"] == board["cells"]
        next_board = community.get_or_create_board(12, bingo.week_key(bingo.local_day() + timedelta(days=7)))
        future_habit = next((cell for cell in next_board["cells"] if cell["id"] == habit_id), None)
        if future_habit:
            assert future_habit["text_en"] == "A new translation"
        created = await worker.save_habit(value, "Un obicei nou", "A new habit")
        assert created["habit"]["id"] != habit_id
        deleted = await worker.delete_habit(value, created["habit"]["id"])
        assert deleted["status"] == "deleted"
        assert community.get_board(12, board["id"])["cells"] == board["cells"]
    asyncio.run(run())


@pytest.mark.parametrize("text_ro,text_en,habit_id", [("", "A", None), ("A", " ", None), ("A" * 201, "B", None), ("A", "B" * 201, None), ("A", "B", 0), ("A", "B", True)])
def test_habit_validation(tmp_db, text_ro, text_en, habit_id):
    original = community.list_habits(12)
    value = interaction(manager=True)
    asyncio.run(feature().save_habit(value, text_ro, text_en, habit_id))
    assert community.list_habits(12) == original
    assert value.followup.send.call_args.args[0] == bingo._t("invalid_habit", "en")


def test_pool_minimum_and_maximum_and_management_message_bounds(tmp_db):
    async def run():
        worker = feature()
        value = interaction(manager=True)
        habits = community.list_habits(12)
        for habit in habits[9:]:
            await worker.delete_habit(value, habit["id"])
        result = await worker.delete_habit(value, habits[0]["id"])
        assert result["status"] == "minimum" and len(community.list_habits(12)) == 9
        assert value.followup.send.call_args.args[0] == bingo._t("minimum", "en")
        for index in range(41):
            community.save_habit(12, f"Habit {index} " + "@everyone *" * 17, "Obicei " + str(index))
        result = await worker.save_habit(value, "Încă unul", "One more")
        assert result["status"] == "limit" and len(community.list_habits(12)) == 50
        assert value.followup.send.call_args.args[0] == bingo._t("limit", "en")
        await worker.save_habit(value, "RO", "EN", habits[0]["id"])
        chunks = value.followup.send.call_args_list
        assert all(len(call.args[0]) <= 2000 and call.kwargs["ephemeral"] for call in chunks)
        assert all("@everyone" not in call.args[0] for call in chunks)
        final_content = "\n".join(call.args[0] for call in chunks)
        assert all(f"#{habit['id']}:" in final_content for habit in community.list_habits(12))
    asyncio.run(run())


def test_wrong_guild_habit_ids_and_board_ids_are_rejected(tmp_db):
    board = community.get_or_create_board(13)
    habit = community.list_habits(13)[0]
    value = interaction(manager=True)

    async def run():
        worker = feature()
        result = await worker.save_habit(value, "RO", "EN", habit["id"])
        assert result["status"] == "missing"
        result = await worker.delete_habit(value, habit["id"])
        assert result["status"] == "missing"
        await worker.handle_control(value, 12, board["id"], 42, 23, "mark", 0)
        assert value.followup.send.call_args.args[0] == bingo.text("expired", "en")
        assert community.get_board(13, board["id"])["marks"] == []
        assert community.list_habits(13)[0] == habit
    asyncio.run(run())


def test_discord_embed_limits_and_safe_user_content(tmp_db):
    for habit in community.list_habits(12):
        community.save_habit(12, "@everyone " + "*" * 190, "@here " + "_" * 194, habit["id"])
    board = community.get_or_create_board(12)
    worker = feature()
    for language in ("en", "ro"):
        embed = worker._render_board(board, language)
        assert len(embed) <= 6000 and len(embed.fields) <= 25
        assert all(len(field.value) <= 1024 for field in embed.fields)
        assert all("@everyone" not in field.value and "@here" not in field.value for field in embed.fields)


@pytest.mark.parametrize("operation", ["show", "control", "save", "delete"])
def test_db_errors_are_private_localized_and_sanitized(tmp_db, monkeypatch, operation):
    board = community.get_or_create_board(12)
    value = interaction(language="ro", manager=True)
    target = {"show": "get_or_create_board", "control": "get_board", "save": "save_habit", "delete": "delete_habit"}[operation]
    monkeypatch.setattr(community, target, MagicMock(side_effect=sqlite3.OperationalError("sensitive content")))
    worker = feature()
    call = worker.bingo(value) if operation == "show" else worker.handle_control(value, 12, board["id"], 42, 23, "mark", 0) if operation == "control" else worker.save_habit(value, "RO", "EN") if operation == "save" else worker.delete_habit(value, 1)
    asyncio.run(call)
    assert value.followup.send.call_args.args[0] == bingo.text("failed", "ro")
    assert value.followup.send.call_args.kwargs["ephemeral"] is True
    assert bingo.record.call_args.args[0] == "failure"
    assert "sensitive content" not in str(bingo.record.call_args)


def test_database_work_follows_deferral_and_existing_response_is_reused(tmp_db, monkeypatch):
    original = community.get_or_create_board
    value = interaction()

    def checked(guild_id, **kwargs):
        value.response.defer.assert_awaited_once()
        return original(guild_id, **kwargs)

    monkeypatch.setattr(community, "get_or_create_board", checked)
    asyncio.run(feature().bingo(value))
    value.response.is_done.return_value = True
    asyncio.run(feature().bingo(value))
    value.response.defer.assert_awaited_once()
