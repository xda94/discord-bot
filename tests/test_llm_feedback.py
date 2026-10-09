import asyncio
import sqlite3
import threading
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import discord
import pytest
from discord import app_commands

import db
from features.llm_feedback import (
    LLMFeedbackFeature,
    THUMBS_DOWN,
    THUMBS_UP,
    _chunk_feedback_report,
)
from llm.feedback import MIN_LIKES_FOR_REVIEW, build_feedback_report


def test_feedback_reactions_are_not_seeded_and_only_requester_can_rate(tmp_db):
    feature = object.__new__(LLMFeedbackFeature)
    feature.bot_id = 999
    message = MagicMock()
    message.id = 12345
    message.guild = SimpleNamespace(id=444)
    message.add_reaction = AsyncMock()

    asyncio.run(
        feature.register_reply(
            message,
            requester_user_id=77,
            category="mention",
            model="discord-bot",
            prompt_version="mention-v1",
        )
    )

    message.add_reaction.assert_not_awaited()

    asyncio.run(
        feature.handle_raw_reaction_add(
            SimpleNamespace(user_id=999, message_id=12345, emoji=THUMBS_UP)
        )
    )
    assert db.get_llm_response_feedback(12345)[2] is None

    asyncio.run(
        feature.handle_raw_reaction_add(
            SimpleNamespace(user_id=88, message_id=12345, emoji=THUMBS_UP)
        )
    )
    assert db.get_llm_response_feedback(12345)[2] is None

    asyncio.run(
        feature.handle_raw_reaction_add(
            SimpleNamespace(user_id=77, message_id=12345, emoji=THUMBS_DOWN)
        )
    )
    assert db.get_llm_response_feedback(12345)[2] == -1
    assert db.get_llm_response_feedback(12345)[3:6] == (
        444,
        "discord-bot",
        "mention-v1",
    )


def test_feedback_summary_is_scoped_to_one_guild(tmp_db):
    db.track_llm_response(
        1, 10, "mention", guild_id=100, model="model-a", prompt_version="v1"
    )
    db.track_llm_response(
        2, 11, "mention", guild_id=100, model="model-a", prompt_version="v1"
    )
    db.track_llm_response(
        3, 12, "mention", guild_id=200, model="model-b", prompt_version="v1"
    )
    assert db.set_llm_response_rating(1, 10, 1)
    assert db.set_llm_response_rating(2, 11, -1)
    assert db.set_llm_response_rating(3, 12, 1)

    assert db.get_llm_feedback_summary(100) == [
        ("mention", "model-a", "v1", 2, 1, 1)
    ]
    report = build_feedback_report(db.get_llm_feedback_summary(100))
    assert report["groups"][0]["ratings"] == 2
    assert report["groups"][0]["likes_needed"] == 9


@pytest.mark.parametrize(
    "up,down,qualified,likes_needed,ready",
    [
        (0, 0, False, 10, False),
        (9, 0, False, 1, False),
        (9, 1, False, 1, True),
        (0, 20, False, 10, True),
        (10, 0, True, 0, True),
        (10, 7, True, 0, True),
        (11, 0, True, 0, True),
    ],
)
def test_feedback_report_preserves_fields_and_distinguishes_likes_from_ratings(
    up, down, qualified, likes_needed, ready
):
    rows = [("mention", "model-a", "v1", up + down, up, down)]
    original = deepcopy(rows)

    report = build_feedback_report(rows)

    assert rows == original
    assert MIN_LIKES_FOR_REVIEW == report["required_likes"] == 10
    assert report["qualified_group_count"] == int(qualified)
    assert report["approval_required"] is True
    group = report["groups"][0]
    assert {key: group[key] for key in (
        "category", "model", "prompt_version", "ratings", "up", "down",
        "approval_percent", "ready_to_compare",
    )} == {
        "category": "mention",
        "model": "model-a",
        "prompt_version": "v1",
        "ratings": up + down,
        "up": up,
        "down": down,
        "approval_percent": round(up / (up + down) * 100, 1) if up + down else 0,
        "ready_to_compare": ready,
    }
    assert group["qualified_for_review"] is qualified
    assert group["likes_needed"] == likes_needed
    assert group["approval_required"] is qualified
    assert bool(group["recommendations"]) is qualified


def test_empty_feedback_report_has_no_qualified_groups():
    assert build_feedback_report(iter(())) == {
        "required_likes": 10,
        "qualified_group_count": 0,
        "approval_required": True,
        "groups": [],
    }


def test_qualified_recommendations_identify_comparisons_and_require_approval():
    rows = [
        ("mention", "model-a", "v1", 12, 10, 2),
        ("mention", "model-c", "v3", 11, 10, 1),
        ("mention", "model-b", "v2", 10, 9, 1),
        ("mention", "small-model", "v0", 1, 1, 0),
        ("summon", "unrelated-model", "v4", 10, 10, 0),
    ]

    report = build_feedback_report(rows)
    recommendations = report["groups"][0]["recommendations"]

    assert report["qualified_group_count"] == 3
    assert report == build_feedback_report(iter(rows))
    assert recommendations == build_feedback_report(reversed(rows))["groups"][-1]["recommendations"]
    inspection, comparison, proposal, limitations = recommendations
    assert "Inspect positive and negative feedback" in inspection
    assert "category=mention, model=model-a, prompt_version=v1" in inspection
    assert "reply text is not stored" in inspection
    assert "model=model-b, prompt_version=v2" in comparison
    assert "model=model-c, prompt_version=v3" in comparison
    assert comparison.index("model=model-b") < comparison.index("model=model-c")
    assert "small-model" not in comparison
    assert "unrelated-model" not in comparison
    assert "exact prompt change or target model" in proposal
    assert "administrator approval before applying" in proposal
    assert "not prompts, reply text, or conversation context" in limitations
    assert "cannot explain" in limitations
    assert "causality" in limitations
    assert "determine a specific improvement" in limitations
    assert "Aggregate metadata cannot diagnose individual answer failures" in limitations
    assert "or establish that another configuration is better" in limitations


def test_single_configuration_recommendations_do_not_invent_an_alternative():
    group = build_feedback_report(
        [("summon", "only-model", "v1", 10, 10, 0)]
    )["groups"][0]

    assert "another explicitly identified model/prompt version" in group["recommendations"][1]
    assert "once comparable feedback is available" in group["recommendations"][1]


@pytest.fixture
def summary_callback():
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    LLMFeedbackFeature(client, tree, 999)
    yield tree.get_command("llm-feedback-summary").callback
    asyncio.run(client.close())


@pytest.fixture
def summary_interaction():
    return SimpleNamespace(
        guild=SimpleNamespace(id=444),
        user=SimpleNamespace(guild_permissions=SimpleNamespace(manage_guild=True)),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _assert_private_messages(sender):
    assert sender.await_count > 0
    for call in sender.await_args_list:
        assert call.kwargs["ephemeral"] is True
        mentions = call.kwargs["allowed_mentions"]
        assert mentions.everyone is False
        assert mentions.roles is False
        assert mentions.users is False
        assert mentions.replied_user is False
        assert 0 < len(call.args[0]) <= 1900


@pytest.mark.parametrize("access", ["dm", "denied", "missing", "none"])
def test_summary_command_checks_authorization_before_reading_database(
    monkeypatch, summary_callback, summary_interaction, access
):
    reader = Mock()
    monkeypatch.setattr(db, "get_llm_feedback_summary", reader)
    if access == "dm":
        summary_interaction.guild = None
    elif access == "denied":
        summary_interaction.user.guild_permissions.manage_guild = False
    elif access == "missing":
        summary_interaction.user = SimpleNamespace()
    else:
        summary_interaction.user.guild_permissions = None

    asyncio.run(summary_callback(summary_interaction))

    reader.assert_not_called()
    summary_interaction.response.defer.assert_not_awaited()
    summary_interaction.followup.send.assert_not_awaited()
    _assert_private_messages(summary_interaction.response.send_message)
    content = summary_interaction.response.send_message.await_args.args[0]
    assert ("only inside a server" if access == "dm" else "Manage Server") in content


@pytest.mark.parametrize(
    "up,down,expected",
    [
        (9, 1, "needs 1 more likes for review"),
        (0, 20, "needs 10 more likes for review"),
        (10, 2, "qualified for review; approval required"),
        (11, 0, "qualified for review; approval required"),
    ],
)
def test_summary_command_reads_strictly_off_thread_and_displays_review_status(
    monkeypatch, summary_callback, summary_interaction, up, down, expected
):
    caller_thread = threading.get_ident()
    reader_threads = []

    def read_summary(guild_id, *, raise_on_error):
        assert guild_id == 444
        assert raise_on_error is True
        reader_threads.append(threading.get_ident())
        return [("mention", "model-a", "v1", up + down, up, down)]

    monkeypatch.setattr(db, "get_llm_feedback_summary", read_summary)

    asyncio.run(summary_callback(summary_interaction))

    assert len(reader_threads) == 1
    assert reader_threads[0] != caller_thread
    summary_interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    summary_interaction.response.send_message.assert_not_awaited()
    _assert_private_messages(summary_interaction.followup.send)
    content = "".join(call.args[0] for call in summary_interaction.followup.send.await_args_list)
    assert f"{up + down} ratings; {up} 👍 / {down} 👎" in content
    assert f"{round(up / (up + down) * 100, 1):.1f}% approval" in content
    assert "ready to compare" in content
    assert expected in content
    assert "Review requires 10 likes" in content
    assert f"{int(up >= 10)} group(s) qualified for review" in content
    assert "No change applied" in content
    assert "Administrator approval is required" in content
    assert ("Inspect positive and negative feedback" in content) is (up >= 10)
    assert ("exact prompt change or target model" in content) is (up >= 10)


def test_summary_command_distinguishes_empty_feedback_from_database_failure(
    monkeypatch, summary_callback, summary_interaction
):
    reader = Mock(return_value=[])
    monkeypatch.setattr(db, "get_llm_feedback_summary", reader)

    asyncio.run(summary_callback(summary_interaction))

    reader.assert_called_once_with(444, raise_on_error=True)
    summary_interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    _assert_private_messages(summary_interaction.followup.send)
    summary_interaction.followup.send.assert_awaited_once()
    assert "No rated LLM replies" in summary_interaction.followup.send.await_args.args[0]


@pytest.mark.parametrize("error_type", [sqlite3.OperationalError, RuntimeError])
def test_summary_command_database_failure_offers_private_retry_without_details(
    monkeypatch, summary_callback, summary_interaction, error_type
):
    reader = Mock(side_effect=error_type("private database details @everyone"))
    monkeypatch.setattr(db, "get_llm_feedback_summary", reader)

    asyncio.run(summary_callback(summary_interaction))

    reader.assert_called_once_with(444, raise_on_error=True)
    summary_interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    summary_interaction.response.send_message.assert_not_awaited()
    _assert_private_messages(summary_interaction.followup.send)
    summary_interaction.followup.send.assert_awaited_once()
    content = summary_interaction.followup.send.await_args.args[0]
    assert "Please retry this command" in content
    assert "No rated" not in content
    assert "private database details" not in content
    assert "@everyone" not in content


@pytest.mark.parametrize(
    "text",
    [
        "",
        "x" * 1900,
        "x" * 1901,
        "x" * 1900 + "\n" + "y",
        "x" * 1899 + "\n" + "y",
        "line\n" * 1000,
        "short\n" + "x" * 5000 + "\nend",
        "\n" + "x" * 4000,
        "👍" * 4000,
    ],
)
def test_report_chunking_bounds_oversized_lines_without_losing_text(text):
    chunks = _chunk_feedback_report(text)

    assert "".join(chunks) == text
    assert all(0 < len(chunk) <= 1900 for chunk in chunks)


def test_report_chunking_keeps_lines_together_when_they_fit():
    first = "a" * 1000 + "\n"
    second = "b" * 1000 + "\n"

    assert _chunk_feedback_report(first + second + "last") == [first, second + "last"]


def test_multipart_summary_is_private_and_disables_all_mentions(
    monkeypatch, summary_callback, summary_interaction
):
    model = "@everyone <@123> <@&456> " + "x" * 4000
    monkeypatch.setattr(
        db,
        "get_llm_feedback_summary",
        Mock(return_value=[
            ("mention", model, "v1", 11, 10, 1),
            ("summon", "other-model", "v2", 15, 5, 10),
        ]),
    )

    asyncio.run(summary_callback(summary_interaction))

    assert summary_interaction.followup.send.await_count > 2
    summary_interaction.response.send_message.assert_not_awaited()
    _assert_private_messages(summary_interaction.followup.send)
    content = "".join(call.args[0] for call in summary_interaction.followup.send.await_args_list)
    assert model in content
    assert "summon | other-model | v2" in content
    assert "No change applied" in content
    assert "Aggregate metadata" in content
    assert "needs 5 more likes for review" in content


@pytest.mark.parametrize("up", [9, 10])
def test_summary_command_reads_persisted_feedback_without_changing_it(
    tmp_db, summary_callback, summary_interaction, up
):
    for message_id in range(1, 12):
        db.track_llm_response(
            message_id,
            77,
            "mention",
            guild_id=444,
            model="model-a",
            prompt_version="v1",
        )
        assert db.set_llm_response_rating(message_id, 77, 1 if message_id <= up else -1)
    before = db.get_llm_feedback_summary(444)

    asyncio.run(summary_callback(summary_interaction))

    content = "".join(call.args[0] for call in summary_interaction.followup.send.await_args_list)
    assert "11 ratings" in content
    assert ("Inspect positive and negative feedback" in content) is (up >= 10)
    assert "No change applied" in content
    _assert_private_messages(summary_interaction.followup.send)
    assert db.get_llm_feedback_summary(444) == before


def test_summary_command_real_reader_propagates_database_failure(
    monkeypatch, summary_callback, summary_interaction
):
    from db import bot_data

    monkeypatch.setattr(
        bot_data,
        "_connect",
        Mock(side_effect=sqlite3.OperationalError("private connection error")),
    )

    asyncio.run(summary_callback(summary_interaction))

    _assert_private_messages(summary_interaction.followup.send)
    content = summary_interaction.followup.send.await_args.args[0]
    assert "Please retry this command" in content
    assert "No rated" not in content
    assert "private connection error" not in content
