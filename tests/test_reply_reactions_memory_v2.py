import asyncio
import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
from discord import app_commands

import db
from llm_client import LlamaCppError
from features.llm_mention import (
    AskJob,
    LLMMentionFeature,
    ReactionJob,
    strip_leading_reply_labels,
)
from features.user_memory import UserMemoryFeature
from tease_llm import (
    MentionResult,
    generate_memory_delta,
    generate_mention_result,
)


def _memory_feature():
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    return client, UserMemoryFeature(client, tree)


def _message(content="hello", *, user_id=7, guild_id=100, channel_id=10):
    return SimpleNamespace(
        author=SimpleNamespace(id=user_id, display_name="Robeeque", bot=False),
        guild=SimpleNamespace(id=guild_id) if guild_id is not None else None,
        channel=SimpleNamespace(id=channel_id),
        clean_content=content,
        content=content,
        add_reaction=AsyncMock(),
    )


def test_reply_label_cleanup_handles_real_prefix_and_preserves_natural_name():
    names = ("Robeeque", "Balen")
    assert strip_leading_reply_labels(
        "@Robeeque Balen: Sigur, iată motivele.", requester_id=123, names=names
    ) == "Sigur, iată motivele."
    assert strip_leading_reply_labels(
        "**@Robeeque Balen:** Sigur.", requester_id=123, names=names
    ) == "Sigur."
    assert strip_leading_reply_labels(
        "<@123> <@123> Balen: Salut.", requester_id=123, names=names
    ) == "Salut."
    assert strip_leading_reply_labels(
        "Balen is relevant to this answer.", requester_id=123, names=names
    ) == "Balen is relevant to this answer."


def test_reply_sender_adds_only_one_requester_mention_with_bot_label():
    feature = object.__new__(LLMMentionFeature)
    feature.feedback = None
    original = SimpleNamespace(reply=AsyncMock())
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Robeeque", name="robeeque"),
        question="hello",
        model="discord-bot",
        reply_to=original,
        bot_names=("Balen",),
    )

    asyncio.run(feature._reply_mention(job, "@Robeeque Balen: Salut!"))

    assert original.reply.await_args.args == ("<@123> Salut!",)


def test_worker_sends_only_corrected_answer_after_labeled_short_echo(monkeypatch):
    feature = object.__new__(LLMMentionFeature)
    feature.feedback = None
    original = SimpleNamespace(reply=AsyncMock(), add_reaction=AsyncMock())
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Robeeque", name="robeeque"),
        question="Esti okay?",
        model="discord-bot",
        reply_to=original,
        bot_names=("Balen",),
    )
    query = MagicMock(side_effect=[
        json.dumps({"text": "<@123> Balen: Ești okay?", "reaction": "👍"}),
        json.dumps({"text": "Da, sunt bine. Tu cum ești?", "reaction": None}),
    ])
    monkeypatch.setattr("tease_llm.query_llm", query)

    asyncio.run(feature._process_job(job))

    original.reply.assert_awaited_once()
    assert original.reply.await_args.args == ("<@123> Da, sunt bine. Tu cum ești?",)
    original.add_reaction.assert_not_awaited()
    assert query.call_count == 2


def test_worker_never_posts_a_repeated_question_after_both_attempts_fail(monkeypatch):
    feature = object.__new__(LLMMentionFeature)
    feature.feedback = None
    original = SimpleNamespace(reply=AsyncMock(), add_reaction=AsyncMock())
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Robeeque"),
        question="De ce te comporti urat cu Schular?",
        model="discord-bot",
        reply_to=original,
    )
    query = MagicMock(return_value=json.dumps({
        "text": "De ce te comporți urât cu Schular?", "reaction": "👍",
    }))
    monkeypatch.setattr("tease_llm.query_llm", query)

    asyncio.run(feature._process_job(job))

    original.reply.assert_awaited_once()
    assert original.reply.await_args.args == (
        "I couldn't generate a reliable answer. Please try again.",
    )
    original.add_reaction.assert_not_awaited()
    assert query.call_count == 2


def test_structured_mention_retries_malformed_and_echoed_outputs(monkeypatch):
    replies = iter(
        [
            "not json",
            '{"text":"Python este un limbaj de programare.","reaction":"👍"}',
        ]
    )
    query = MagicMock(side_effect=lambda *args, **kwargs: next(replies))
    monkeypatch.setattr("tease_llm.query_llm", query)

    result = generate_mention_result("Robeeque", "Ce este Python?")

    assert result == MentionResult("Python este un limbaj de programare.", "👍")
    assert query.call_count == 2
    assert "previous output was invalid" in query.call_args.kwargs["prompt"].lower()

    replies = iter(
        [
            '{"text":"please explain this exact sentence now","reaction":null}',
            '{"text":"Could you clarify which part you want explained?","reaction":null}',
        ]
    )
    monkeypatch.setattr(
        "tease_llm.query_llm", MagicMock(side_effect=lambda *args, **kwargs: next(replies))
    )
    result = generate_mention_result(
        "Alice", "please explain this exact sentence now"
    )
    assert result.text.startswith("Could you clarify")


def test_structured_mention_retries_empty_but_not_connection_failure(monkeypatch):
    calls = iter(
        [
            LlamaCppError("llama.cpp returned an empty response."),
            '{"text":"Răspuns complet.","reaction":null}',
        ]
    )

    def query(*args, **kwargs):
        result = next(calls)
        if isinstance(result, Exception):
            raise result
        return result

    mocked = MagicMock(side_effect=query)
    monkeypatch.setattr("tease_llm.query_llm", mocked)
    assert generate_mention_result("Robeeque", "Poți răspunde?").text == "Răspuns complet."
    assert mocked.call_count == 2

    mocked = MagicMock(side_effect=LlamaCppError("connection refused"))
    monkeypatch.setattr("tease_llm.query_llm", mocked)
    assert generate_mention_result("Robeeque", "Poți răspunde?") is None
    assert mocked.call_count == 1


def test_romanian_followup_prompt_contains_conversation_context(monkeypatch):
    query = MagicMock(return_value='{"text":"Da, este mai rapid.","reaction":null}')
    monkeypatch.setattr("tease_llm.query_llm", query)

    result = generate_mention_result(
        "Robeeque",
        "Și consumul?",
        context_messages=["User: Vorbeam despre noul model local.", "Bot: Este rapid."],
        replied_message="Alex: Modelul folosește aproximativ 4 GB RAM.",
    )

    assert result.text == "Da, este mai rapid."
    prompt = query.call_args.kwargs["prompt"]
    assert "Vorbeam despre noul model local" in prompt
    assert "Modelul folosește aproximativ 4 GB RAM" in prompt
    assert "Și consumul?" in prompt


def test_ordinary_reaction_sampling_cooldown_and_busy_skip(monkeypatch):
    feature = object.__new__(LLMMentionFeature)
    feature._processing = False
    feature._queue = SimpleNamespace(qsize=lambda: 0)
    feature._reaction_pending_channels = set()
    feature._reaction_last_attempt = {}
    feature._put_job = AsyncMock(return_value=True)
    message = _message("Am terminat proiectul!")
    monkeypatch.setattr("features.llm_mention.random.random", lambda: 0.0)
    monkeypatch.setattr("features.llm_mention.get_selected_model", lambda: "discord-bot")

    assert asyncio.run(feature.handle_ordinary_message(message)) is True
    queued = feature._put_job.await_args.args[1]
    assert isinstance(queued, ReactionJob)
    assert queued.message is message

    feature._reaction_pending_channels.clear()
    feature._put_job.reset_mock()
    assert asyncio.run(feature.handle_ordinary_message(message)) is False
    feature._put_job.assert_not_awaited()

    feature._reaction_last_attempt.clear()
    feature._processing = True
    assert asyncio.run(feature.handle_ordinary_message(message)) is False


def test_reaction_permission_failure_is_nonfatal(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="discord_bot")
    feature = object.__new__(LLMMentionFeature)
    response = SimpleNamespace(status=403, reason="Forbidden")
    message = _message("Bravo!")
    message.add_reaction.side_effect = discord.Forbidden(response, "missing permission")
    monkeypatch.setattr(
        "features.llm_mention.generate_ordinary_reaction",
        lambda *args, **kwargs: "🎉",
    )

    asyncio.run(
        feature._process_reaction_job(
            ReactionJob(message=message, model="discord-bot", channel_id=10)
        )
    )
    message.add_reaction.assert_awaited_once_with("🎉")
    assert caplog.records[-1].exc_info is None


def test_no_reaction_choice_does_not_touch_message(monkeypatch):
    feature = object.__new__(LLMMentionFeature)
    message = _message("routine status update")
    monkeypatch.setattr(
        "features.llm_mention.generate_ordinary_reaction",
        lambda *args, **kwargs: None,
    )

    asyncio.run(
        feature._process_reaction_job(
            ReactionJob(message=message, model="discord-bot", channel_id=10)
        )
    )
    message.add_reaction.assert_not_awaited()


def test_mention_result_reacts_to_original_message(monkeypatch):
    feature = object.__new__(LLMMentionFeature)
    feature.memory = None
    feature._reaction_pending_channels = set()
    feature._reaction_last_attempt = {}
    feature._reply_mention = AsyncMock(return_value="Felicitări!")
    feature._reply_job_error = AsyncMock()
    original = SimpleNamespace(
        channel=SimpleNamespace(id=10), add_reaction=AsyncMock(), reply=AsyncMock()
    )
    job = AskJob(
        user=SimpleNamespace(id=7, display_name="Robeeque"),
        question="Am terminat proiectul!",
        model="discord-bot",
        reply_to=original,
    )
    monkeypatch.setattr(
        "features.llm_mention.generate_mention_result",
        lambda *args, **kwargs: MentionResult("Felicitări!", "🎉"),
    )

    asyncio.run(feature._process_job(job))

    original.add_reaction.assert_awaited_once_with("🎉")
    feature._reply_mention.assert_awaited_once_with(job, "Felicitări!")


def test_mention_reaction_permission_failure_is_nonfatal(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="discord_bot")
    feature = object.__new__(LLMMentionFeature)
    feature.memory = None
    feature._reaction_pending_channels = set()
    feature._reaction_last_attempt = {}
    feature._reply_mention = AsyncMock(return_value="Felicitări!")
    feature._reply_job_error = AsyncMock()
    response = SimpleNamespace(status=403, reason="Forbidden")
    original = SimpleNamespace(
        channel=SimpleNamespace(id=10),
        add_reaction=AsyncMock(
            side_effect=discord.Forbidden(response, "missing permission")
        ),
    )
    job = AskJob(
        user=SimpleNamespace(id=7, display_name="Robeeque"),
        question="Am terminat proiectul!",
        model="discord-bot",
        reply_to=original,
    )
    monkeypatch.setattr(
        "features.llm_mention.generate_mention_result",
        lambda *args, **kwargs: MentionResult("Felicitări!", "🎉"),
    )

    asyncio.run(feature._process_job(job))

    feature._reply_mention.assert_awaited_once_with(job, "Felicitări!")
    assert caplog.records[-1].exc_info is None


def test_memory_rows_accumulate_beyond_old_limit_and_correct_one_entry(tmp_db):
    additions = [
        {
            "kind": "fact",
            "content": f"Durable preference {index}: " + "x" * 240,
            "source_text": f"source {index}",
        }
        for index in range(20)
    ]
    assert db.apply_llm_memory_delta(100, 7, additions, (), ())
    rows = db.get_llm_memory_entries(100, 7)
    assert sum(len(row[2]) for row in rows) > 4000
    assert all(row[3].startswith("sha256:") and "source" not in row[3] for row in rows)
    target_id = rows[-1][0]

    correction = {
        "id": target_id,
        "kind": "fact",
        "content": "Corrected durable preference",
        "source_text": "I prefer this now",
    }
    assert db.apply_llm_memory_delta(100, 7, (), (correction,), ())
    updated = db.get_llm_memory_entries(100, 7)
    assert any(row[0] == target_id and row[2] == correction["content"] for row in updated)
    assert len(updated) == 20


def test_memory_delta_validates_source_and_target(monkeypatch):
    existing = [{"id": 5, "kind": "fact", "content": "Prefers Python"}]
    monkeypatch.setattr(
        "tease_llm.query_llm",
        lambda *args, **kwargs: (
            '{"add":[],"correct":[{"id":5,"kind":"fact",'
            '"content":"Prefers Rust","source_index":0}]}'
        ),
    )
    result = generate_memory_delta(existing, ["I prefer Rust now"], model="discord-bot")
    assert result.successful is True
    assert result.corrections[0]["source_text"] == "I prefer Rust now"

    monkeypatch.setattr(
        "tease_llm.query_llm",
        lambda *args, **kwargs: (
            '{"add":[],"correct":[{"id":99,"kind":"fact",'
            '"content":"Prefers Rust","source_index":0}]}'
        ),
    )
    assert generate_memory_delta(existing, ["I prefer Rust"], model="discord-bot").successful is False


def test_memory_delta_prompt_and_schema_use_available_source_indexes(monkeypatch):
    captured = {}

    def query(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["schema"] = kwargs["response_schema"]
        return '{"add":[],"correct":[]}'

    monkeypatch.setattr("tease_llm.query_llm", query)

    result = generate_memory_delta(
        [], ["First observation", "Second observation"], model="discord-bot"
    )

    assert result.successful is True
    assert '"source_index": 0, "content": "First observation"' in captured["prompt"]
    assert '"source_index": 1, "content": "Second observation"' in captured["prompt"]
    assert "fact, impression, like, dislike, or topic" in captured["prompt"]
    for key in ("add", "correct"):
        properties = captured["schema"]["properties"][key]["items"]["properties"]
        source_index = properties["source_index"]
        assert source_index["minimum"] == 0
        assert source_index["maximum"] == 1
        assert properties["kind"]["enum"] == [
            "fact",
            "impression",
            "like",
            "dislike",
            "topic",
        ]


def test_memory_delta_accepts_profile_synthesis_categories(monkeypatch):
    monkeypatch.setattr(
        "tease_llm.query_llm",
        lambda *args, **kwargs: (
            '{"add":['
            '{"kind":"like","content":"Likes tea","source_index":0},'
            '{"kind":"impression","content":"Seems methodical","source_index":1}'
            '],"correct":[]}'
        ),
    )

    result = generate_memory_delta(
        [], ["I love tea", "I check every step twice"], model="discord-bot"
    )

    assert result.successful is True
    assert [entry["kind"] for entry in result.additions] == ["like", "impression"]


def test_raw_transcript_is_not_persisted(tmp_db):
    assert db.add_llm_memory_transcript(100, 7, 10, "user", "private chat") is False
    assert db.get_llm_memory_transcript(100, 7) == []


def test_old_profile_migrates_once_with_stable_rows(tmp_db):
    db.set_llm_user_memory(100, 7, "# Preferences\n- Likes Python\n1. Owns a cat")
    db.init_db()
    first = db.get_llm_memory_entries(100, 7)
    db.init_db()
    second = db.get_llm_memory_entries(100, 7)

    assert [(row[0], row[2]) for row in first] == [(row[0], row[2]) for row in second]
    assert {row[2] for row in first} == {
        "Preferences: Likes Python",
        "Preferences: Owns a cat",
    }
    assert db.get_llm_user_memory(100, 7) is None


def test_pending_observations_survive_restart_and_become_eligible(tmp_db):
    db.set_llm_memory_channel_enabled(100, 10, True)
    client, feature = _memory_feature()
    message = _message("I am building a Discord bot")
    asyncio.run(feature.handle_message(message))
    asyncio.run(client.close())

    client2, restored = _memory_feature()
    batches = restored.eligible_batches(now=time.time() + 24 * 60 * 60 + 1)
    assert len(batches) == 1
    assert batches[0].observations == ("I am building a Discord bot",)
    asyncio.run(client2.close())


def test_commit_rejects_change_without_supporting_user_message(tmp_db):
    db.set_llm_memory_channel_enabled(100, 10, True)
    client, feature = _memory_feature()
    message = _message("I prefer Rust")
    asyncio.run(feature.handle_message(message))
    batch = feature.context_for(message).batch

    assert feature.commit_delta(
        batch,
        ({"kind": "fact", "content": "Prefers Go", "source_text": "invented"},),
        (),
    ) is False
    assert db.get_llm_memory_entries(100, 7) == []
    assert feature.context_for(message).batch is not None
    asyncio.run(client.close())


def test_forget_deletes_entries_and_pending_observations(tmp_db):
    db.set_llm_memory_channel_enabled(100, 10, True)
    db.apply_llm_memory_delta(
        100,
        7,
        ({"kind": "fact", "content": "Likes Python", "source_text": "source"},),
        (),
        (),
    )
    db.add_llm_memory_observation(100, 7, 100, 10, "pending")

    assert db.delete_all_llm_user_memory(100, 7) == 2
    assert db.get_llm_memory_entries(100, 7) == []
    assert db.get_llm_memory_transcript(100, 7) == []
    assert db.get_llm_memory_observations(100, 7) == []
