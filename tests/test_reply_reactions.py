import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from llm.client import LlamaCppError
from features.llm_mention import (
    AskJob,
    LLMMentionFeature,
    ReactionJob,
    strip_leading_reply_labels,
)
from llm.responses import (
    MentionResult,
    generate_mention_result,
)


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
    names = ("Robeeque", "Nova")
    assert strip_leading_reply_labels(
        "@Robeeque Nova: Sigur, iată motivele.", requester_id=123, names=names
    ) == "Sigur, iată motivele."
    assert strip_leading_reply_labels(
        "**@Robeeque Nova:** Sigur.", requester_id=123, names=names
    ) == "Sigur."
    assert strip_leading_reply_labels(
        "<@123> <@123> Nova: Salut.", requester_id=123, names=names
    ) == "Salut."
    assert strip_leading_reply_labels(
        "Nova is relevant to this answer.", requester_id=123, names=names
    ) == "Nova is relevant to this answer."


def test_reply_sender_adds_only_one_requester_mention_with_bot_label():
    feature = object.__new__(LLMMentionFeature)
    original = SimpleNamespace(reply=AsyncMock())
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Robeeque", name="robeeque"),
        question="hello",
        model="discord-bot",
        reply_to=original,
        bot_names=("Nova",),
    )

    asyncio.run(feature._reply_mention(job, "@Robeeque Nova: Salut!"))

    assert original.reply.await_args.args == ("<@123> Salut!",)


def test_worker_sends_only_corrected_answer_after_labeled_short_echo(monkeypatch):
    feature = object.__new__(LLMMentionFeature)
    original = SimpleNamespace(reply=AsyncMock(), add_reaction=AsyncMock())
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Robeeque", name="robeeque"),
        question="Esti okay?",
        model="discord-bot",
        reply_to=original,
        bot_names=("Nova",),
    )
    query = MagicMock(side_effect=[
        json.dumps({"text": "<@123> Nova: Ești okay?", "reaction": "👍"}),
        json.dumps({"text": "Da, sunt bine. Tu cum ești?", "reaction": None}),
    ])
    monkeypatch.setattr("llm.client.query_llm", query)

    asyncio.run(feature._process_job(job))

    original.reply.assert_awaited_once()
    assert original.reply.await_args.args == ("<@123> Da, sunt bine. Tu cum ești?",)
    original.add_reaction.assert_not_awaited()
    assert query.call_count == 2


def test_worker_never_posts_a_repeated_question_after_both_attempts_fail(monkeypatch):
    feature = object.__new__(LLMMentionFeature)
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
    monkeypatch.setattr("llm.client.query_llm", query)

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
    monkeypatch.setattr("llm.client.query_llm", query)

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
        "llm.client.query_llm", MagicMock(side_effect=lambda *args, **kwargs: next(replies))
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
    monkeypatch.setattr("llm.client.query_llm", mocked)
    assert generate_mention_result("Robeeque", "Poți răspunde?").text == "Răspuns complet."
    assert mocked.call_count == 2

    mocked = MagicMock(side_effect=LlamaCppError("connection refused"))
    monkeypatch.setattr("llm.client.query_llm", mocked)
    assert generate_mention_result("Robeeque", "Poți răspunde?") is None
    assert mocked.call_count == 1


def test_romanian_followup_prompt_contains_conversation_context(monkeypatch):
    query = MagicMock(return_value='{"text":"Da, este mai rapid.","reaction":null}')
    monkeypatch.setattr("llm.client.query_llm", query)

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


