import asyncio
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from db import community
from features import social_games as games
from features.humor_common import QUESTS


def interaction(user_id=42, guild_id=12, channel_id=23, message_id=99, language="en"):
    guild = SimpleNamespace(id=guild_id)
    user = SimpleNamespace(id=user_id, bot=False, guild=guild)
    guild.get_member = MagicMock(return_value=user)
    guild.fetch_member = AsyncMock(return_value=user)
    message = SimpleNamespace(id=message_id, edit=AsyncMock())
    channel = SimpleNamespace(id=channel_id, guild=guild, send=AsyncMock(return_value=message), fetch_message=AsyncMock(return_value=message))
    response = SimpleNamespace(is_done=MagicMock(return_value=False), defer=AsyncMock(), send_message=AsyncMock())
    return SimpleNamespace(user=user, guild=guild, guild_id=guild_id, channel=channel, channel_id=channel_id, message=message, response=response, followup=SimpleNamespace(send=AsyncMock()), locale=language)


def feature(value=None):
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    worker = games.SocialGamesFeature(client, tree)
    client.add_view = MagicMock()
    if value:
        client.get_channel = MagicMock(return_value=value.channel)
        client.fetch_channel = AsyncMock(return_value=value.channel)
    return worker


def quest_payload():
    chosen = QUESTS[0]
    return {"challenge_en": chosen["text_en"], "challenge_ro": chosen["text_ro"], "title_key": chosen["title_key"], "title_en": chosen["title_en"], "title_ro": chosen["title_ro"]}


def session(kind="sedinta", phase="active", deadline=None, creator=42, bound=True):
    payload = {"subject": "Lunch", "options": ["Tea", "Coffee"]} if kind == "sedinta" else {"accusation": "Biscuit theft"} if kind == "tribunal" else quest_payload()
    row = community.create_session(12, 23, creator, kind, "en", payload, subject_id=45 if kind == "tribunal" else None, now=time.time() - 100)
    if bound:
        community.bind_session_message(12, row["id"], 99)
    if kind == "tribunal" and phase == "pending":
        with community._connect(commit=True) as cursor:
            cursor.execute("UPDATE community_sessions SET phase='pending',deadline=? WHERE id=?", (time.time() + 86400, row["id"]))
    if kind == "quest" and phase in ("active", "verification"):
        community.transition_session(12, row["id"], "pending", "active", creator)
        if phase == "verification":
            community.transition_session(12, row["id"], "active", "verification", creator)
    if deadline is not None:
        with community._connect(commit=True) as cursor:
            cursor.execute("UPDATE community_sessions SET deadline=? WHERE id=?", (deadline, row["id"]))
    return community.get_session(12, row["id"])


def approved_quote():
    row = community.create_quote_request(12, 23, 345, 42, 45, "The biscuits have escaped", "Ada", "en")
    return community.approve_quote(12, row["id"], 45)["quote"]


@pytest.fixture(autouse=True)
def external_mocks(monkeypatch):
    monkeypatch.setattr(games, "record", AsyncMock())
    monkeypatch.setattr(games, "generate_humor", AsyncMock(side_effect=lambda kind, language, facts, fallback, **kwargs: fallback))


def test_command_registration():
    worker = feature()
    assert {command.name for command in worker.tree.get_commands()} == {"tribunal", "meeting", "quest"}
    assert all(command.guild_only for command in worker.tree.get_commands())


@pytest.mark.parametrize("language", ["en", "ro"])
def test_tribunal_immediate_vote_and_safe_mentions(tmp_db, language):
    value = interaction(language=language)
    accused = SimpleNamespace(id=45, bot=False, guild=value.guild)
    row = asyncio.run(feature(value).tribunal(value, accused, "@everyone **biscuit theft**"))
    assert row["phase"] == "active" and row["language"] == language
    assert row["message_id"] == 99
    assert row["deadline"] == row["created_at"] + 600
    assert community.cast_vote(12, row["id"], 60, "guilty")["status"] == "changed"
    sent = value.channel.send.call_args
    assert "<@45>" in sent.args[0]
    assert "@everyone" not in sent.args[0]
    assert sent.kwargs["allowed_mentions"].everyone is False
    assert sent.kwargs["allowed_mentions"].roles is False
    assert [user.id for user in sent.kwargs["allowed_mentions"].users] == [45]
    assert sent.kwargs["suppress_embeds"] is True
    assert [button.custom_id for button in sent.kwargs["view"].children] == [f"social:{row['id']}:guilty", f"social:{row['id']}:innocent"]
    games.generate_humor.assert_not_called()


@pytest.mark.parametrize("kind", ["tribunal", "quest"])
def test_owner_only_consent(tmp_db, kind):
    row = session(kind, phase="pending", bound=kind != "quest")
    value = interaction(user_id=60)
    asyncio.run(feature(value).handle_control(value, 12, row["id"], "accept"))
    assert community.get_session(12, row["id"])["phase"] == "pending"
    value.channel.send.assert_not_called()
    assert value.followup.send.call_args.args[0] == games.text("not_owner", "en")


def test_tribunal_acceptance_starts_exact_ten_minutes(tmp_db, monkeypatch):
    row = session("tribunal", phase="pending")
    value = interaction(user_id=45)
    now = time.time()
    monkeypatch.setattr(community.time, "time", lambda: now)
    asyncio.run(feature(value).handle_control(value, 12, row["id"], "accept"))
    changed = community.get_session(12, row["id"])
    assert changed["phase"] == "active" and changed["deadline"] == now + 600
    assert "<@45>" in value.message.edit.call_args.kwargs["content"]
    assert value.message.edit.call_args.kwargs["allowed_mentions"].users is False
    assert {button.custom_id.rsplit(":", 1)[1] for button in value.message.edit.call_args.kwargs["view"].children} == {"guilty", "innocent"}
    assert games.record.call_args_list[0].args[:2] == ("control", "community/accept")


def test_tribunal_all_members_can_vote_immediately(tmp_db):
    async def run():
        accused = interaction(user_id=45)
        worker = feature(accused)
        row = await worker.tribunal(accused, accused.user, "Biscuit theft")
        voting = accused.channel.send.call_args.kwargs["view"]
        assert all(not button.disabled for button in voting.children)
        for user_id in (42, 45, 60, 61):
            voter = interaction(user_id=user_id)
            await voting.children[0].callback(voter)
            assert voter.followup.send.call_args.args[0] == games.text("saved", "en")
        assert community.session_tally(12, row["id"]) == {"guilty": 4, "innocent": 0}
        await voting.children[1].callback(interaction(user_id=60))
        assert community.session_tally(12, row["id"]) == {"guilty": 3, "innocent": 1}
    asyncio.run(run())


def test_decline_has_no_model(tmp_db):
    row = session("tribunal", phase="pending")
    value = interaction(user_id=45)
    asyncio.run(feature(value).handle_control(value, 12, row["id"], "decline"))
    changed = community.get_session(12, row["id"])
    assert changed["phase"] == "declined"
    assert value.message.edit.call_args.kwargs["content"] == games._t("declined", "en")
    assert all(button.disabled for button in value.message.edit.call_args.kwargs["view"].children)
    games.generate_humor.assert_not_called()


@pytest.mark.parametrize("options", ["A", "A|a", "A||B", "A|B|C|D|E|F", "x" * 81 + "|B"])
def test_meeting_validates_options(tmp_db, options):
    value = interaction()
    assert asyncio.run(feature(value).meeting(value, "Lunch", options)) is None
    assert community.list_sessions(12) == []


def test_meeting_public_binding_and_deferred_reuse(tmp_db):
    value = interaction()
    value.response.is_done.return_value = True
    row = asyncio.run(feature(value).meeting(value, "Lunch", " Tea | Coffee "))
    assert row["payload"]["options"] == ["Tea", "Coffee"] and row["message_id"] == 99
    value.response.defer.assert_not_called()


@pytest.mark.parametrize("kind", ["tribunal", "sedinta"])
@pytest.mark.parametrize("votes,expected", [([], "Neutral result"), (["0", "1"], "Neutral result"), (["0", "0", "1"], "Result:")])
def test_authoritative_ties_and_counts(tmp_db, kind, votes, expected):
    row = session(kind)
    keys = ["guilty", "innocent"] if kind == "tribunal" else ["0", "1"]
    for index, option in enumerate(votes):
        community.cast_vote(12, row["id"], 60 + index, keys[int(option)])
    with community._connect(commit=True) as cursor:
        cursor.execute("UPDATE community_sessions SET deadline=0 WHERE id=?", (row["id"],))
    value = interaction()
    asyncio.run(feature(value)._finish_session(row))
    content = value.message.edit.call_args.kwargs["content"]
    assert expected in content
    assert f"{dict(games._options(row))[keys[0]]}: {votes.count('0')}" in content
    assert f"{dict(games._options(row))[keys[1]]}: {votes.count('1')}" in content
    assert community.get_session(12, row["id"])["phase"] == "closed"
    games.generate_humor.assert_awaited_once()
    assert games.generate_humor.call_args.kwargs["background"] is True


def test_votes_change_and_scope_checks(tmp_db):
    row = session()
    value = interaction(user_id=60)
    worker = feature(value)
    asyncio.run(worker.handle_control(value, 12, row["id"], "0"))
    asyncio.run(worker.handle_control(value, 12, row["id"], "1"))
    assert community.session_tally(12, row["id"]) == {"0": 0, "1": 1}
    for field, invalid in (("guild_id", 77), ("channel_id", 88)):
        other = interaction(user_id=61)
        setattr(other, field, invalid)
        asyncio.run(worker.handle_control(other, 12, row["id"], "0"))
    wrong_message = interaction(user_id=62, message_id=199)
    asyncio.run(worker.handle_control(wrong_message, 12, row["id"], "0"))
    bot = interaction(user_id=63)
    bot.user.bot = True
    asyncio.run(worker.handle_control(bot, 12, row["id"], "0"))
    assert community.session_tally(12, row["id"]) == {"0": 0, "1": 1}


def test_quest_offer_private_then_single_public_acceptance(tmp_db):
    async def run():
        value = interaction()
        worker = feature(value)
        row = await worker.quest(value)
        assert row["message_id"] is None and row["phase"] == "pending"
        value.channel.send.assert_not_called()
        assert value.followup.send.call_args.kwargs["ephemeral"] is True
        assert "<@42>" in value.followup.send.call_args.args[0]
        await asyncio.gather(worker.handle_control(value, 12, row["id"], "accept"), worker.handle_control(value, 12, row["id"], "accept"))
        current = community.get_session(12, row["id"])
        assert current["phase"] == "active" and current["message_id"] == 99
        assert 604795 < current["deadline"] - time.time() <= 604800
        value.channel.send.assert_awaited_once()
        assert "<@42>" in value.channel.send.call_args.args[0]
        assert "<@42>" in value.message.edit.call_args.kwargs["content"]
        assert all(button.disabled for button in value.channel.send.call_args.kwargs["view"].children)
        assert all(not button.disabled for button in value.message.edit.call_args.kwargs["view"].children)
        repeated = await worker.quest(value)
        assert repeated["id"] == row["id"]
        value.channel.send.assert_awaited_once()
    asyncio.run(run())
    games.generate_humor.assert_not_called()


def test_private_quest_requires_actual_membership(tmp_db):
    row = session("quest", phase="pending", bound=False)
    value = interaction()
    value.guild.get_member.return_value = None
    value.guild.fetch_member.side_effect = discord.NotFound(SimpleNamespace(status=404, reason="missing"), "missing")
    asyncio.run(feature(value).handle_control(value, 12, row["id"], "accept"))
    assert community.get_session(12, row["id"])["phase"] == "cancelled"
    value.channel.send.assert_not_called()


@pytest.mark.parametrize("language", ["en", "ro"])
def test_assigned_quest_recipient_controls_and_title(tmp_db, language):
    async def run():
        assigner = interaction(language=language)
        recipient = interaction(user_id=45, language=language)
        worker = feature(assigner)
        await worker.tree.get_command("quest").callback(assigner, recipient.user)
        row = community.list_sessions(12)[0]
        assert row["creator_id"] == assigner.user.id and row["subject_id"] == recipient.user.id
        assert row["phase"] == "pending" and row["message_id"] == 99
        assert row["deadline"] == row["created_at"] + 86400
        sent = assigner.channel.send.call_args
        assert "<@45>" in sent.args[0] and row["payload"]["challenge_" + language] in sent.args[0]
        assert [user.id for user in sent.kwargs["allowed_mentions"].users] == [45]
        assert sent.kwargs["allowed_mentions"].everyone is False and sent.kwargs["allowed_mentions"].roles is False
        await sent.kwargs["view"].children[0].callback(assigner)
        assert community.get_session(12, row["id"])["phase"] == "pending"
        assert assigner.followup.send.call_args.args[0] == games.text("not_owner", language)
        restored = feature(assigner)
        await restored.handle_control(recipient, 12, row["id"], "accept")
        current = community.get_session(12, row["id"])
        assert current["phase"] == "active" and current["message_id"] == 99
        assert 604795 < current["deadline"] - time.time() <= 604800
        recipient.channel.send.assert_not_called()
        await restored.handle_control(assigner, 12, row["id"], "completed")
        assert community.get_session(12, row["id"])["phase"] == "active"
        await restored.handle_control(recipient, 12, row["id"], "completed")
        assert community.get_session(12, row["id"])["phase"] == "verification"
        await restored.handle_control(recipient, 12, row["id"], "success")
        assert community.session_tally(12, row["id"]) == {"success": 0, "not_yet": 0}
        assert community.cast_vote(12, row["id"], recipient.user.id, "success")["status"] == "forbidden"
        await restored.handle_control(assigner, 12, row["id"], "success")
        with community._connect(commit=True) as cursor:
            cursor.execute("UPDATE community_sessions SET deadline=0 WHERE id=?", (row["id"],))
        await restored._finish_session(row)
        assert len(community.list_titles(12, recipient.user.id)) == 1
        assert community.list_titles(12, assigner.user.id) == []
    asyncio.run(run())
    games.generate_humor.assert_not_called()


@pytest.mark.parametrize("action,phase", [("decline", "declined"), ("abandon", "cancelled")])
def test_assigned_quest_can_be_declined_or_abandoned(tmp_db, action, phase):
    async def run():
        assigner, recipient = interaction(), interaction(user_id=45)
        worker = feature(assigner)
        row = await worker.quest(assigner, recipient.user)
        if action == "abandon":
            await worker.handle_control(recipient, 12, row["id"], "accept")
        await worker.handle_control(assigner, 12, row["id"], action)
        assert community.get_session(12, row["id"])["phase"] == ("active" if action == "abandon" else "pending")
        await worker.handle_control(recipient, 12, row["id"], action)
        assert community.get_session(12, row["id"])["phase"] == phase
    asyncio.run(run())


def test_assigned_quest_reuses_recipient_quest_without_affecting_assigner(tmp_db):
    async def run():
        assigner, recipient, other = interaction(), interaction(user_id=45), interaction(user_id=60)
        worker = feature(assigner)
        own = await worker.quest(assigner)
        row = await worker.quest(assigner, recipient.user)
        assert row["id"] != own["id"]
        assert (await worker.quest(other, recipient.user))["id"] == row["id"]
        assert (await worker.quest(recipient))["id"] == row["id"]
        assert "view" not in recipient.followup.send.call_args.kwargs
        assert "https://discord.com/channels/12/23/99" in recipient.followup.send.call_args.args[0]
        assert (await worker.quest(assigner, assigner.user))["id"] == own["id"]
        assert community.create_session(12, 23, recipient.user.id, "quest", "en", quest_payload())["id"] == row["id"]
        assigner.channel.send.assert_awaited_once()
        other.channel.send.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("invalid", ["bot", "system", "guild"])
def test_assigned_quest_rejects_invalid_recipient(tmp_db, invalid):
    value = interaction()
    recipient = interaction(user_id=45).user
    if invalid == "guild":
        recipient.guild.id = 13
    else:
        setattr(recipient, invalid, True)
    assert asyncio.run(feature(value).quest(value, recipient)) is None
    assert community.list_sessions(12) == []
    value.channel.send.assert_not_called()


def test_assigned_quest_send_failure_cancels_invitation(tmp_db):
    value = interaction()
    value.channel.send.side_effect = OSError("failed")
    assert asyncio.run(feature(value).quest(value, interaction(user_id=45).user)) is None
    assert community.list_sessions(12)[0]["phase"] == "cancelled"


@pytest.mark.parametrize("success,negative,title", [(0, 0, False), (1, 1, False), (1, 2, False), (2, 1, True)])
def test_quest_verification_and_idempotent_symbolic_award(tmp_db, success, negative, title):
    row = session("quest", phase="active")
    value = interaction()
    worker = feature(value)
    asyncio.run(worker.handle_control(value, 12, row["id"], "completed"))
    assert community.get_session(12, row["id"])["phase"] == "verification"
    asyncio.run(worker.handle_control(value, 12, row["id"], "success"))
    assert community.session_tally(12, row["id"]) == {"success": 0, "not_yet": 0}
    for index in range(success + negative):
        community.cast_vote(12, row["id"], 60 + index, "success" if index < success else "not_yet")
    with community._connect(commit=True) as cursor:
        cursor.execute("UPDATE community_sessions SET deadline=0 WHERE id=?", (row["id"],))
    asyncio.run(worker._finish_session(row))
    asyncio.run(worker._finish_session(row))
    assert len(community.list_titles(12, 42)) == int(title)
    content = value.message.edit.call_args.kwargs["content"]
    assert ("Symbolic title earned" in content) is title
    games.generate_humor.assert_not_called()


def test_titles_offer_another_without_creating_until_click(tmp_db):
    row = session("quest", phase="verification")
    community.cast_vote(12, row["id"], 60, "success")
    with community._connect(commit=True) as cursor:
        cursor.execute("UPDATE community_sessions SET deadline=0 WHERE id=?", (row["id"],))
    value = interaction()
    worker = feature(value)
    asyncio.run(worker._finish_session(row))
    assert asyncio.run(worker.quest(value)) is None
    assert "Ambassador of Kind Words" in value.followup.send.call_args.args[0]
    assert isinstance(value.followup.send.call_args.kwargs["view"], games.QuestAgainView)
    assert asyncio.run(worker.quest(value, offer_new=True))["phase"] == "pending"


@pytest.mark.parametrize("kind,phase", [("tribunal", "pending"), ("quest", "pending"), ("quest", "active")])
def test_expiration_never_generates(tmp_db, kind, phase):
    row = session(kind, phase=phase, deadline=0, bound=phase != "pending" or kind == "tribunal")
    value = interaction(user_id=45 if kind == "tribunal" else 42)
    worker = feature(value)
    asyncio.run(worker.handle_control(value, 12, row["id"], "accept"))
    assert community.get_session(12, row["id"])["phase"] == phase
    asyncio.run(worker._tick())
    assert community.get_session(12, row["id"])["phase"] == "expired"
    games.generate_humor.assert_not_called()


def test_send_failure_cancels_invisible_vote(tmp_db):
    value = interaction()
    value.channel.send.side_effect = OSError("sensitive payload")
    assert asyncio.run(feature(value).meeting(value, "secret subject", "A|B")) is None
    row = community.list_sessions(12)[0]
    assert row["phase"] == "cancelled" and row["message_id"] is None
    assert "secret" not in value.followup.send.call_args.args[0]


def test_quest_send_failure_cancels_offer(tmp_db):
    row = session("quest", phase="pending", bound=False)
    value = interaction()
    value.channel.send.side_effect = OSError("failed")
    asyncio.run(feature(value).handle_control(value, 12, row["id"], "accept"))
    assert community.get_session(12, row["id"])["phase"] == "cancelled"


@pytest.mark.parametrize("kind", ["sedinta", "quest"])
def test_binding_failure_disables_already_sent_message(tmp_db, monkeypatch, kind):
    value = interaction()
    worker = feature(value)
    monkeypatch.setattr(community, "bind_session_message", MagicMock(side_effect=sqlite3.OperationalError("failed")))
    if kind == "quest":
        row = session("quest", phase="pending", bound=False)
        asyncio.run(worker.handle_control(value, 12, row["id"], "accept"))
    else:
        asyncio.run(worker.meeting(value, "Lunch", "A|B"))
        row = community.list_sessions(12)[0]
    current = community.get_session(12, row["id"])
    assert current["phase"] == "cancelled" and current["message_id"] is None
    assert value.message.edit.call_args.kwargs["content"] == games._t("cancelled", "en")
    assert all(button.disabled for button in value.message.edit.call_args.kwargs["view"].children)


def test_active_control_edit_retries_before_voting_deadline(tmp_db, monkeypatch):
    row = session("tribunal", phase="pending")
    value = interaction(user_id=45)
    value.message.edit.side_effect = [OSError("connection"), None]
    worker = feature(value)
    asyncio.run(worker.handle_control(value, 12, row["id"], "accept"))
    current = community.get_session(12, row["id"])
    assert current["phase"] == "active" and current["delivery_status"] == "retry"
    now = current["retry_at"] + 1
    assert now < current["deadline"]
    monkeypatch.setattr(community.time, "time", lambda: now)
    asyncio.run(worker._tick())
    assert community.get_session(12, row["id"])["delivery_status"] == "delivered"
    assert value.message.edit.await_count == 2
    assert {button.custom_id.rsplit(":", 1)[1] for button in value.message.edit.call_args.kwargs["view"].children} == {"guilty", "innocent"}
    games.generate_humor.assert_not_called()


def test_model_failure_and_restart_do_not_generate_twice(tmp_db, monkeypatch):
    row = session(deadline=0)
    value = interaction()
    worker = feature(value)
    monkeypatch.setattr(games, "generate_humor", AsyncMock(side_effect=RuntimeError("private content")))
    original_save = community.save_session_result
    monkeypatch.setattr(community, "save_session_result", MagicMock(side_effect=sqlite3.OperationalError("unavailable")))
    asyncio.run(worker._tick())
    frozen = community.get_session(12, row["id"])
    assert frozen["phase"] == "closing" and frozen["generation_started_at"] is not None
    fallback = frozen["result_text"]
    monkeypatch.setattr(community, "save_session_result", original_save)
    asyncio.run(feature(value)._tick())
    final = community.get_session(12, row["id"])
    assert final["phase"] == "closed" and final["result_text"] == fallback
    games.generate_humor.assert_awaited_once()
    value.channel.send.assert_not_called()


def test_transient_result_edit_retries_same_message_and_result(tmp_db, monkeypatch):
    row = session(deadline=0)
    value = interaction()
    value.message.edit.side_effect = [OSError("connection"), None]
    worker = feature(value)
    asyncio.run(worker._tick())
    first = community.get_session(12, row["id"])
    assert first["delivery_status"] == "retry" and first["error_code"] == "http"
    content = value.message.edit.call_args.kwargs["content"]
    now = first["retry_at"] + 1
    monkeypatch.setattr(community.time, "time", lambda: now)
    asyncio.run(feature(value)._tick())
    final = community.get_session(12, row["id"])
    assert final["delivery_status"] == "delivered" and final["result_text"] == first["result_text"]
    assert value.message.edit.call_args.kwargs["content"] == content
    assert all(call.args == (99,) for call in value.channel.fetch_message.call_args_list)
    games.generate_humor.assert_awaited_once()
    value.channel.send.assert_not_called()


@pytest.mark.parametrize("error_type,code", [(discord.NotFound, "not_found"), (discord.Forbidden, "forbidden")])
def test_terminal_discord_delivery_error(tmp_db, error_type, code):
    row = session(deadline=0)
    value = interaction()
    value.channel.fetch_message.side_effect = error_type(SimpleNamespace(status=404 if code == "not_found" else 403, reason=code), "secret")
    worker = feature(value)
    asyncio.run(worker._tick())
    current = community.get_session(12, row["id"])
    assert current["delivery_status"] == "error" and current["error_code"] == code
    asyncio.run(worker._tick())
    value.channel.fetch_message.assert_awaited_once()


def test_tick_database_failure_does_not_block_other_sessions_or_next_tick(tmp_db, monkeypatch):
    first = session(deadline=0)
    second = session(deadline=0)
    real_get = community.get_session
    def get(guild_id, session_id):
        if session_id == first["id"]:
            raise sqlite3.OperationalError("private data")
        return real_get(guild_id, session_id)
    monkeypatch.setattr(community, "get_session", get)
    value = interaction()
    worker = feature(value)
    asyncio.run(worker._tick())
    assert real_get(12, second["id"])["phase"] == "closed"
    monkeypatch.setattr(community, "get_session", real_get)
    asyncio.run(worker._tick())
    assert real_get(12, first["id"])["phase"] == "closed"


def test_restore_idempotent_and_overdue_closes_promptly(tmp_db):
    async def run():
        row = session("tribunal", phase="pending", deadline=0)
        current = session()
        value = interaction()
        worker = feature(value)
        worker._check.start = MagicMock()
        worker._check.is_running = MagicMock(side_effect=[False, True])
        await worker.start_tasks()
        await worker.start_tasks()
        assert community.get_session(12, row["id"])["phase"] == "expired"
        assert worker.client.add_view.call_count == 2
        worker._check.start.assert_called_once()
        assert any(call.kwargs["message_id"] == current["message_id"] for call in worker.client.add_view.call_args_list)
    asyncio.run(run())


def test_tick_paginates_past_first_hundred(tmp_db):
    for index in range(101):
        session(deadline=0)
    value = interaction()
    asyncio.run(feature(value)._tick())
    assert len(community.list_due_sessions()) == 0
    assert value.message.edit.await_count == 101


def test_quiz_approval_hidden_author_and_changeable_answers(tmp_db):
    quote = approved_quote()
    value = interaction()
    worker = feature(value)
    value.response.is_done.return_value = True
    row = asyncio.run(worker.create_quiz(value, quote, [{"user_id": 45, "name": "Ada"}, {"user_id": 46, "name": "Bea"}]))
    value.response.defer.assert_not_called()
    assert "Correct author" not in value.channel.send.call_args.args[0]
    assert quote["quote_text"] in value.channel.send.call_args.args[0]
    voter = interaction(user_id=60)
    asyncio.run(worker.handle_control(voter, 12, row["id"], "45"))
    asyncio.run(worker.handle_control(voter, 12, row["id"], "46"))
    with community._connect(commit=True) as cursor:
        cursor.execute("UPDATE community_sessions SET deadline=0 WHERE id=?", (row["id"],))
    asyncio.run(worker._tick())
    content = value.message.edit.call_args.kwargs["content"]
    assert "Correct author: Ada" in content and "Ada: 0" in content and "Bea: 1" in content
    assert community.list_titles(12) == []
    games.generate_humor.assert_not_called()


def test_quote_revocation_clears_quiz_before_close_and_delivery(tmp_db):
    quote = approved_quote()
    value = interaction()
    worker = feature(value)
    row = asyncio.run(worker.create_quiz(value, quote, [{"user_id": 45, "name": "Ada"}, {"user_id": 46, "name": "Bea"}]))
    community.delete_quote(12, quote["id"])
    asyncio.run(worker._tick())
    current = community.get_session(12, row["id"])
    assert current["phase"] == "cancelled" and current["payload"] == {} and current["result_text"] is None
    assert value.message.edit.call_args.kwargs["content"] == games._t("quote_removed", "en")
    games.generate_humor.assert_not_called()


def test_quote_revocation_during_close_prevents_stale_result(tmp_db, monkeypatch):
    quote = approved_quote()
    value = interaction()
    worker = feature(value)
    row = asyncio.run(worker.create_quiz(value, quote, [{"user_id": 45, "name": "Ada"}, {"user_id": 46, "name": "Bea"}]))
    with community._connect(commit=True) as cursor:
        cursor.execute("UPDATE community_sessions SET deadline=0 WHERE id=?", (row["id"],))
    save = community.save_session_result
    def revoke(guild_id, session_id, result):
        community.delete_quote(12, quote["id"])
        return save(guild_id, session_id, result)
    monkeypatch.setattr(community, "save_session_result", revoke)
    asyncio.run(worker._tick())
    assert community.get_session(12, row["id"])["result_text"] is None
    assert "escaped" not in value.message.edit.call_args.kwargs["content"]


def test_quote_revocation_during_result_edit_reconciles_same_message(tmp_db):
    quote = approved_quote()
    value = interaction()
    worker = feature(value)
    row = asyncio.run(worker.create_quiz(value, quote, [{"user_id": 45, "name": "Ada"}, {"user_id": 46, "name": "Bea"}]))
    with community._connect(commit=True) as cursor:
        cursor.execute("UPDATE community_sessions SET deadline=0 WHERE id=?", (row["id"],))
    async def edit(**kwargs):
        if "Correct author" in kwargs["content"]:
            community.delete_quote(12, quote["id"])
    value.message.edit.side_effect = edit
    asyncio.run(worker._tick())
    current = community.get_session(12, row["id"])
    assert current["payload"] == {} and current["result_text"] is None
    assert current["delivery_status"] == "delivered"
    assert value.message.edit.await_count == 2
    assert value.message.edit.call_args.kwargs["content"] == games._t("quote_removed", "en")
    assert value.message.edit.call_args.kwargs["view"].children == []
    value.channel.send.assert_awaited_once()


def test_quote_revocation_during_failed_edit_keeps_reconciliation_pending(tmp_db):
    quote = approved_quote()
    value = interaction()
    worker = feature(value)
    row = asyncio.run(worker.create_quiz(value, quote, [{"user_id": 45, "name": "Ada"}, {"user_id": 46, "name": "Bea"}]))
    with community._connect(commit=True) as cursor:
        cursor.execute("UPDATE community_sessions SET deadline=0 WHERE id=?", (row["id"],))
    async def edit(**kwargs):
        community.delete_quote(12, quote["id"])
        raise OSError("connection")
    value.message.edit.side_effect = edit
    asyncio.run(worker._tick())
    current = community.get_session(12, row["id"])
    assert current["delivery_status"] == "pending" and current["retry_at"] is None
    value.message.edit.side_effect = None
    asyncio.run(worker._tick())
    assert community.get_session(12, row["id"])["delivery_status"] == "delivered"
    assert value.message.edit.call_args.kwargs["content"] == games._t("quote_removed", "en")


def test_quiz_rejects_unapproved_quote(tmp_db):
    quote = community.create_quote_request(12, 23, 345, 42, 45, "private quote", "Ada", "en")
    value = interaction()
    assert asyncio.run(feature(value).create_quiz(value, quote, [{"user_id": 45, "name": "Ada"}, {"user_id": 46, "name": "Bea"}])) is None
    value.channel.send.assert_not_called()
    assert community.list_sessions(12) == []


def test_cancelled_session_reconciles_disabled_controls(tmp_db):
    row = session()
    community.cancel_session(12, row["id"])
    value = interaction()
    asyncio.run(feature(value)._tick())
    assert value.message.edit.call_args.kwargs["content"] == games._t("cancelled", "en")
    assert all(button.disabled for button in value.message.edit.call_args.kwargs["view"].children)


def test_restoration_paginates_all_active_work(tmp_db):
    async def run():
        for index in range(101):
            session()
        worker = feature(interaction())
        worker._check.start = MagicMock()
        await worker.start_tasks()
        assert worker.client.add_view.call_count == 101
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["tribunal", "sedinta", "quest"])
def test_view_ids_persistent_and_bounded(tmp_db, kind):
    async def run():
        row = session(kind, phase="pending" if kind != "sedinta" else "active")
        view = games.SessionView(feature(), row)
        assert view.timeout is None and view.is_persistent()
        assert all(len(button.custom_id) <= 100 for button in view.children)
    asyncio.run(run())
