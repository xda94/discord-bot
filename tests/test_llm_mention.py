import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import requests
import discord
from discord import app_commands

from llm_client import LlamaCppError, get_default_model, query_llm
from features.llm_mention import (
    AskJob,
    LLMMentionFeature,
    MemoryJob,
    DISCORD_MESSAGE_LIMIT,
    DISCORD_SAFE_LIMIT,
    MEMORY_MENTION_PROMPT_VERSION,
    MAX_IMAGE_BYTES,
    REFERENCE_CONTEXT_CHAR_BUDGET,
    IMAGE_DESCRIPTION_REQUEST,
    VISION_MEMORY_MENTION_PROMPT_VERSION,
    VISION_MENTION_PROMPT_VERSION,
    budget_reference_context,
    detect_image_mime,
    get_ask_cooldown_seconds,
    get_memory_consolidation_interval_seconds,
    get_selected_model,
    select_image_attachment,
    split_discord_messages,
)
from features.user_memory import MemoryBatch


PNG_BYTES = b"\x89PNG\r\n\x1a\nimage"
JPEG_BYTES = b"\xff\xd8\xffimage"


def _attachment(
    *,
    filename="image.png",
    content_type="image/png",
    size=len(PNG_BYTES),
    width=100,
    height=100,
    data=PNG_BYTES,
):
    return SimpleNamespace(
        filename=filename,
        content_type=content_type,
        size=size,
        width=width,
        height=height,
        read=AsyncMock(return_value=data),
    )


def _mention_message(*, text="", attachments=None, user_id=123):
    bot = SimpleNamespace(id=999888777, display_name="Bot")
    content = f"<@{bot.id}> {text}".strip()
    return SimpleNamespace(
        author=SimpleNamespace(id=user_id, display_name="Alice"),
        content=content,
        clean_content=content,
        mentions=[bot],
        role_mentions=[],
        channel_mentions=[],
        attachments=list(attachments or []),
        channel=SimpleNamespace(),
        guild=None,
        reply=AsyncMock(),
    )


def _handling_feature(*, processing=False, queue_size=0):
    feature = object.__new__(LLMMentionFeature)
    feature.bot_id = 999888777
    feature.memory = None
    feature._user_pending = set()
    feature._user_last_ask = {}
    feature._processing = processing
    feature._queue = SimpleNamespace(qsize=lambda: queue_size)
    feature._enqueue_job = AsyncMock(return_value=True)
    return feature


@pytest.mark.parametrize("summon_only", [False, True])
@pytest.mark.parametrize("label", ["", "Robeeque: "])
def test_mention_reply_tags_requester_and_preserves_feedback(summon_only, label):
    feature = object.__new__(LLMMentionFeature)
    feature.feedback = SimpleNamespace(register_reply=AsyncMock())
    original = SimpleNamespace(reply=AsyncMock())
    user = SimpleNamespace(id=123, display_name="Robeeque")
    job = AskJob(
        user=user, question="hello", model="discord-bot", reply_to=original,
        summon_only=summon_only,
    )

    asyncio.run(feature._reply_mention(job, label + "Salut!"))

    sent = original.reply.await_args
    assert sent.args == ("<@123> Salut!",)
    assert sent.kwargs["mention_author"] is False
    assert sent.kwargs["allowed_mentions"].to_dict() == {
        "parse": [], "users": [123]
    }
    feature.feedback.register_reply.assert_awaited_once()
    feedback = feature.feedback.register_reply.await_args
    assert feedback.args[0] is original.reply.return_value
    assert feedback.kwargs["requester_user_id"] == 123
    assert feedback.kwargs["category"] == ("summon" if summon_only else "mention")


def test_long_mention_reply_only_pings_requester_in_first_chunk():
    feature = object.__new__(LLMMentionFeature)
    feature.feedback = None
    original = SimpleNamespace(reply=AsyncMock())
    channel = SimpleNamespace(send=AsyncMock())
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Robeeque"),
        question="hello", model="discord-bot", reply_to=original, channel=channel,
    )
    text = "@everyone <@456> <@&789> " + "x" * 5000

    asyncio.run(feature._reply_mention(job, text))

    first = original.reply.await_args
    chunks = [first.args[0]] + [call.args[0] for call in channel.send.await_args_list]
    assert len(chunks) > 1
    assert chunks[0].startswith("<@123> ")
    # The existing splitter trims whitespace at chunk boundaries.
    assert "".join(chunks).replace(" ", "") == ("<@123> " + text).replace(" ", "")
    assert all(len(chunk) <= DISCORD_SAFE_LIMIT for chunk in chunks)
    assert first.kwargs["allowed_mentions"].to_dict() == {"parse": [], "users": [123]}
    for call in channel.send.await_args_list:
        assert call.kwargs["reference"] is original
        assert call.kwargs["allowed_mentions"].to_dict() == {"parse": []}


def test_get_ask_cooldown_seconds(monkeypatch):
    monkeypatch.setenv("ASK_COOLDOWN_SECONDS", "90")
    assert get_ask_cooldown_seconds() == 90.0


def test_get_memory_consolidation_interval_seconds(monkeypatch):
    monkeypatch.delenv("LLM_MEMORY_CONSOLIDATION_INTERVAL_SECONDS", raising=False)
    assert get_memory_consolidation_interval_seconds() == 300.0

    monkeypatch.setenv("LLM_MEMORY_CONSOLIDATION_INTERVAL_SECONDS", "900")
    assert get_memory_consolidation_interval_seconds() == 900.0


def test_memory_scheduler_uses_configured_interval(monkeypatch):
    class SchedulerStopped(Exception):
        pass

    sleep_delays = []

    async def fake_sleep(delay):
        sleep_delays.append(delay)
        if len(sleep_delays) > 1:
            raise SchedulerStopped

    feature = object.__new__(LLMMentionFeature)
    feature.memory = SimpleNamespace(eligible_batches=MagicMock(return_value=[]))
    feature._model_busy = MagicMock(return_value=False)
    feature._memory_last_finished = None
    monkeypatch.setenv("LLM_MEMORY_CONSOLIDATION_INTERVAL_SECONDS", "900")
    monkeypatch.setattr("features.llm_mention.asyncio.sleep", fake_sleep)

    with pytest.raises(SchedulerStopped):
        asyncio.run(feature._memory_scheduler_loop())

    assert sleep_delays == [900.0, 900.0]
    feature.memory.eligible_batches.assert_called_once_with()


def test_memory_scheduler_admits_only_one_batch_per_scan(monkeypatch):
    class SchedulerStopped(Exception):
        pass

    calls = 0

    async def fake_sleep(_delay):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise SchedulerStopped

    first = SimpleNamespace(scope_id=1, user_id=1)
    second = SimpleNamespace(scope_id=1, user_id=2)
    feature = object.__new__(LLMMentionFeature)
    feature.memory = SimpleNamespace(
        eligible_batches=MagicMock(return_value=[first, second])
    )
    feature._enqueue_memory = AsyncMock(return_value=True)
    feature._model_busy = MagicMock(return_value=False)
    feature._memory_last_finished = None
    monkeypatch.setattr("features.llm_mention.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "features.llm_mention.get_selected_model", lambda: "discord-bot"
    )

    with pytest.raises(SchedulerStopped):
        asyncio.run(feature._memory_scheduler_loop())

    feature._enqueue_memory.assert_awaited_once_with(first, "discord-bot")


def test_worker_admission_does_not_build_a_backlog():
    async def scenario():
        feature = object.__new__(LLMMentionFeature)
        feature._processing = False
        feature._queue = asyncio.PriorityQueue()
        feature._queue_sequence = 0
        feature._ensure_worker = MagicMock()

        first = await feature._put_job(0, "first")
        second = await feature._put_job(0, "second")

        assert first is True
        assert second is False
        assert feature._queue.qsize() == 1
        assert feature._queue.get_nowait()[2] == "first"

    asyncio.run(scenario())


@pytest.mark.parametrize("busy,last_finished,now,scans", [
    (True, None, 1000.0, 0),
    (False, 999.0, 1000.0, 0),
    (False, 700.0, 1000.0, 1),
])
def test_memory_scan_skips_active_work_and_waits_after_completion(
    monkeypatch, busy, last_finished, now, scans
):
    class StopScan(Exception):
        pass

    sleep = AsyncMock(side_effect=[None, StopScan])
    feature = object.__new__(LLMMentionFeature)
    feature.memory = SimpleNamespace(eligible_batches=MagicMock(return_value=[]))
    feature._model_busy = MagicMock(return_value=busy)
    feature._memory_last_finished = last_finished
    monkeypatch.setenv("LLM_MEMORY_CONSOLIDATION_INTERVAL_SECONDS", "300")
    monkeypatch.setattr("features.llm_mention.asyncio.sleep", sleep)
    monkeypatch.setattr("features.llm_mention.time.monotonic", lambda: now)

    with pytest.raises(StopScan):
        asyncio.run(feature._memory_scheduler_loop())
    assert feature.memory.eligible_batches.call_count == scans


def test_failed_memory_worker_releases_slot_and_starts_rest_interval(tmp_db):
    async def scenario():
        client = discord.Client(intents=discord.Intents.none())
        feature = LLMMentionFeature(client, app_commands.CommandTree(client), bot_id=99)
        batch = MemoryBatch(100, 7, 100, 10, 0, 1, ("hello",))
        feature._process_memory_job = AsyncMock(side_effect=LlamaCppError("timeout"))
        try:
            assert await feature._enqueue_memory(batch, "discord-bot")
            await asyncio.wait_for(feature._queue.join(), timeout=1)
            assert not feature._model_busy()
            assert not feature._memory_pending
            assert feature._memory_last_finished is not None
            assert not feature._worker_task.done()
        finally:
            feature._worker_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await feature._worker_task
            await client.close()

    asyncio.run(scenario())


def test_stale_stored_model_is_replaced_with_llama_cpp_default(tmp_db):
    import db

    db.set_setting("mention_model", "old-ollama-model")

    assert get_selected_model() == "discord-bot"
    assert db.get_setting("mention_model") == "discord-bot"


def test_image_signature_detection():
    assert detect_image_mime(PNG_BYTES) == "image/png"
    assert detect_image_mime(JPEG_BYTES) == "image/jpeg"
    assert detect_image_mime(b"RIFF-webp") is None


def test_image_selection_uses_first_supported_direct_image():
    first = _attachment(filename="first.jpg", content_type="image/jpeg")
    second = _attachment(filename="second.png")

    attachment, mime, count, error = select_image_attachment([first, second])

    assert attachment is first
    assert mime == "image/jpeg"
    assert count == 2
    assert error is None


@pytest.mark.parametrize(
    ("attachment", "message"),
    [
        (
            _attachment(filename="image.webp", content_type="image/webp"),
            "only PNG or JPEG",
        ),
        (_attachment(size=MAX_IMAGE_BYTES + 1), "under 8 MiB"),
        (_attachment(width=5001, height=5001), "under 25 megapixels"),
        (_attachment(width=None), "validate that image's dimensions"),
    ],
)
def test_image_selection_rejects_unsupported_or_excessive_images(
    attachment, message
):
    selected, mime, count, error = select_image_attachment([attachment])

    assert selected is None
    assert mime is None
    assert count == 1
    assert message in error


@pytest.mark.parametrize(
    ("text", "expected_question"),
    [
        ("", IMAGE_DESCRIPTION_REQUEST),
        ("What error does this show?", "What error does this show?"),
    ],
)
def test_image_mention_enqueues_description_or_caption(
    monkeypatch, text, expected_question
):
    monkeypatch.setattr("features.llm_mention.get_selected_model", lambda: "discord-bot")
    feature = _handling_feature()
    attachment = _attachment()
    message = _mention_message(text=text, attachments=[attachment])

    assert asyncio.run(feature.handle_message(message)) is True

    job = feature._enqueue_job.await_args.args[0]
    assert job.question == expected_question
    assert job.summon_only is False
    assert job.image_attachment is attachment
    assert job.image_mime == "image/png"
    assert job.prompt_version == VISION_MENTION_PROMPT_VERSION
    attachment.read.assert_not_awaited()
    message.reply.assert_not_awaited()


def test_busy_multi_image_request_is_not_queued(monkeypatch):
    monkeypatch.setattr("features.llm_mention.get_selected_model", lambda: "discord-bot")
    feature = _handling_feature(processing=True, queue_size=1)
    first = _attachment(filename="first.png")
    second = _attachment(filename="second.jpg", content_type="image/jpeg")
    message = _mention_message(attachments=[first, second])

    asyncio.run(feature.handle_message(message))

    feature._enqueue_job.assert_not_awaited()
    notice = message.reply.await_args.args[0]
    assert "didn't queue" in notice
    assert message.reply.await_args.kwargs["mention_author"] is False


def test_mention_losing_admission_race_is_not_queued(monkeypatch):
    monkeypatch.setattr("features.llm_mention.get_selected_model", lambda: "discord-bot")
    feature = _handling_feature()
    feature._enqueue_job.return_value = False
    message = _mention_message(text="hello")

    asyncio.run(feature.handle_message(message))

    feature._enqueue_job.assert_awaited_once()
    assert "became busy" in message.reply.await_args.args[0]


def test_pending_user_is_not_allowed_to_enqueue_another_image(monkeypatch):
    monkeypatch.setattr("features.llm_mention.get_selected_model", lambda: "discord-bot")
    feature = _handling_feature()
    feature._user_pending.add(123)
    message = _mention_message(attachments=[_attachment()])

    asyncio.run(feature.handle_message(message))

    feature._enqueue_job.assert_not_awaited()
    assert "already working" in message.reply.await_args.args[0]


def test_image_mention_uses_vision_memory_feedback_version(monkeypatch):
    monkeypatch.setattr("features.llm_mention.get_selected_model", lambda: "discord-bot")
    feature = _handling_feature()
    feature.memory = SimpleNamespace(
        context_for=MagicMock(
            return_value=SimpleNamespace(
                enabled=True,
                profile="m" * 4000,
                batch=None,
            )
        )
    )
    message = _mention_message(attachments=[_attachment()])

    asyncio.run(feature.handle_message(message))

    job = feature._enqueue_job.await_args.args[0]
    assert job.prompt_version == VISION_MEMORY_MENTION_PROMPT_VERSION
    assert len(job.user_memory) == 4000


def test_invalid_image_is_rejected_before_queueing(monkeypatch):
    feature = _handling_feature()
    message = _mention_message(
        attachments=[_attachment(filename="image.webp", content_type="image/webp")]
    )

    asyncio.run(feature.handle_message(message))

    feature._enqueue_job.assert_not_awaited()
    assert "only PNG or JPEG" in message.reply.await_args.args[0]


def test_reference_budget_prioritizes_memory_and_newest_history():
    memory, history = budget_reference_context(
        "m" * 4000,
        ["old:" + "x" * 1500, "new:" + "y" * 1500],
    )
    assert len(memory) == 4000
    assert len(memory) + sum(len(item) for item in history) <= 6000
    assert len(history) == 1
    assert history[0].startswith("new:")


def test_reference_budget_counts_escaped_prompt_size():
    memory, history = budget_reference_context("<" * 2000, [">" * 2000])
    escaped_size = len(memory.replace("<", "&lt;")) + sum(
        len(item.replace(">", "&gt;")) for item in history
    )
    assert escaped_size <= REFERENCE_CONTEXT_CHAR_BUDGET


def test_memory_enabled_reply_uses_distinct_feedback_prompt_version():
    feature = object.__new__(LLMMentionFeature)
    feature.feedback = SimpleNamespace(register_reply=AsyncMock())
    original = SimpleNamespace(reply=AsyncMock())
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Robeeque"),
        question="hello",
        model="discord-bot",
        reply_to=original,
        memory_enabled=True,
        prompt_version=MEMORY_MENTION_PROMPT_VERSION,
    )

    asyncio.run(feature._reply_mention(job, "Salut!"))

    assert (
        feature.feedback.register_reply.await_args.kwargs["prompt_version"]
        == MEMORY_MENTION_PROMPT_VERSION
    )


def test_reply_does_not_trigger_memory_synthesis(monkeypatch):
    events = []
    feature = object.__new__(LLMMentionFeature)
    feature.memory = SimpleNamespace(
        can_process_batch=MagicMock(return_value=True),
    )
    feature._reply_mention = AsyncMock(side_effect=lambda *args: events.append("reply"))
    feature._enqueue_memory = AsyncMock(side_effect=lambda *args: events.append("memory"))
    batch = MemoryBatch(
        scope_id=100,
        user_id=123,
        guild_id=100,
        request_channel_id=10,
        generation=0,
        through_sequence=1,
        observations=("I like Python",),
    )
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Robeeque"),
        question="hello",
        model="discord-bot",
        memory_enabled=True,
        memory_batch=batch,
    )
    monkeypatch.setattr(
        "features.llm_mention.generate_mention_result",
        lambda *args, **kwargs: SimpleNamespace(text="Hi", reaction=None),
    )
    asyncio.run(feature._process_job(job))

    assert events == ["reply"]
    feature._enqueue_memory.assert_not_awaited()


def test_failed_memory_consolidation_does_not_commit(monkeypatch):
    feature = object.__new__(LLMMentionFeature)
    feature.memory = SimpleNamespace(
        can_process_batch=MagicMock(return_value=True),
        entries_for_batch=MagicMock(return_value=[]),
        commit_delta=MagicMock(),
    )
    batch = MemoryBatch(
        scope_id=100,
        user_id=123,
        guild_id=100,
        request_channel_id=10,
        generation=0,
        through_sequence=1,
        observations=("hello",),
    )
    monkeypatch.setattr(
        "features.llm_mention.generate_memory_delta",
        lambda *args, **kwargs: SimpleNamespace(
            successful=False, additions=(), corrections=()
        ),
    )

    asyncio.run(feature._process_memory_job(MemoryJob(batch, "discord-bot")))

    feature.memory.commit_delta.assert_not_called()


def test_memory_job_processes_only_one_chunk_per_scheduler_pass(monkeypatch):
    first = MemoryBatch(
        scope_id=100,
        user_id=123,
        guild_id=100,
        request_channel_id=10,
        generation=0,
        through_sequence=1,
        observations=("first",),
    )
    second = MemoryBatch(
        scope_id=100,
        user_id=123,
        guild_id=100,
        request_channel_id=10,
        generation=0,
        through_sequence=2,
        observations=("second",),
    )
    feature = object.__new__(LLMMentionFeature)
    feature.memory = SimpleNamespace(
        synthesis_chunks=MagicMock(return_value=(first, second)),
        can_process_batch=MagicMock(return_value=True),
        entries_for_batch=MagicMock(return_value=[]),
        commit_delta=MagicMock(return_value=True),
    )
    generate = MagicMock(
        return_value=SimpleNamespace(successful=True, additions=(), corrections=())
    )
    monkeypatch.setattr("features.llm_mention.generate_memory_delta", generate)

    asyncio.run(feature._process_memory_job(MemoryJob(first, "discord-bot")))

    generate.assert_called_once()
    assert generate.call_args.args[1] == ["first"]
    feature.memory.commit_delta.assert_called_once_with(first, (), ())


def test_vision_job_downloads_only_in_worker_without_early_memory_synthesis(monkeypatch):
    feature = object.__new__(LLMMentionFeature)
    feature.feedback = None
    feature._reply_mention = AsyncMock()
    feature._reply_job_error = AsyncMock()
    feature.memory = SimpleNamespace(
        can_process_batch=MagicMock(return_value=True)
    )
    feature._enqueue_memory = AsyncMock()
    attachment = _attachment()
    batch = MemoryBatch(
        scope_id=100,
        user_id=123,
        guild_id=100,
        request_channel_id=10,
        generation=0,
        through_sequence=1,
        observations=("Please describe this",),
    )
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Alice"),
        question="Please describe this",
        model="discord-bot",
        reply_to=SimpleNamespace(reply=AsyncMock()),
        image_attachment=attachment,
        image_mime="image/png",
        memory_batch=batch,
    )
    reply = MagicMock(
        return_value=SimpleNamespace(text="A blue diagram.", reaction=None)
    )
    monkeypatch.setattr("features.llm_mention.llama_supports_vision", lambda: True)
    monkeypatch.setattr("features.llm_mention.generate_mention_result", reply)

    asyncio.run(feature._process_job(job))

    attachment.read.assert_awaited_once_with()
    assert reply.call_args.kwargs["image_bytes"] == PNG_BYTES
    assert reply.call_args.kwargs["image_mime"] == "image/png"
    feature._reply_mention.assert_awaited_once_with(job, "A blue diagram.")
    feature._reply_job_error.assert_not_awaited()
    feature._enqueue_memory.assert_not_awaited()


def test_vision_job_stops_before_download_when_projector_is_disabled(monkeypatch):
    feature = object.__new__(LLMMentionFeature)
    feature._reply_job_error = AsyncMock()
    attachment = _attachment()
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Alice"),
        question=IMAGE_DESCRIPTION_REQUEST,
        model="discord-bot",
        image_attachment=attachment,
        image_mime="image/png",
    )
    monkeypatch.setattr("features.llm_mention.llama_supports_vision", lambda: False)

    asyncio.run(feature._process_job(job))

    attachment.read.assert_not_awaited()
    assert "vision support is disabled" in feature._reply_job_error.await_args.args[1]


def test_vision_job_stops_before_download_when_capability_check_fails(monkeypatch):
    feature = object.__new__(LLMMentionFeature)
    feature._reply_job_error = AsyncMock()
    attachment = _attachment()
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Alice"),
        question=IMAGE_DESCRIPTION_REQUEST,
        model="discord-bot",
        image_attachment=attachment,
        image_mime="image/png",
    )

    def unavailable():
        raise LlamaCppError("server unavailable")

    monkeypatch.setattr("features.llm_mention.llama_supports_vision", unavailable)

    asyncio.run(feature._process_job(job))

    attachment.read.assert_not_awaited()
    assert "vision service is unavailable" in feature._reply_job_error.await_args.args[1]


def test_vision_job_rechecks_downloaded_size(monkeypatch):
    feature = object.__new__(LLMMentionFeature)
    feature._reply_job_error = AsyncMock()
    attachment = _attachment(data=PNG_BYTES + b"x" * MAX_IMAGE_BYTES)
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Alice"),
        question=IMAGE_DESCRIPTION_REQUEST,
        model="discord-bot",
        image_attachment=attachment,
        image_mime="image/png",
    )
    monkeypatch.setattr("features.llm_mention.llama_supports_vision", lambda: True)

    asyncio.run(feature._process_job(job))

    assert "exceeds the 8 MiB limit" in feature._reply_job_error.await_args.args[1]


def test_vision_job_reports_deleted_attachment(monkeypatch):
    feature = object.__new__(LLMMentionFeature)
    feature._reply_job_error = AsyncMock()
    attachment = _attachment()
    attachment.read.side_effect = RuntimeError("deleted")
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Alice"),
        question=IMAGE_DESCRIPTION_REQUEST,
        model="discord-bot",
        image_attachment=attachment,
        image_mime="image/png",
    )
    monkeypatch.setattr("features.llm_mention.llama_supports_vision", lambda: True)

    asyncio.run(feature._process_job(job))

    assert "upload it again" in feature._reply_job_error.await_args.args[1]


def test_vision_job_rejects_spoofed_image_bytes(monkeypatch):
    feature = object.__new__(LLMMentionFeature)
    feature._reply_job_error = AsyncMock()
    attachment = _attachment(data=b"not an image")
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Alice"),
        question=IMAGE_DESCRIPTION_REQUEST,
        model="discord-bot",
        image_attachment=attachment,
        image_mime="image/png",
    )
    monkeypatch.setattr("features.llm_mention.llama_supports_vision", lambda: True)

    asyncio.run(feature._process_job(job))

    assert "not a valid PNG or JPEG" in feature._reply_job_error.await_args.args[1]


def test_vision_inference_failure_gets_user_facing_reply(monkeypatch):
    feature = object.__new__(LLMMentionFeature)
    feature._reply_job_error = AsyncMock()
    feature.memory = None
    attachment = _attachment()
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Alice"),
        question=IMAGE_DESCRIPTION_REQUEST,
        model="discord-bot",
        image_attachment=attachment,
        image_mime="image/png",
    )
    monkeypatch.setattr("features.llm_mention.llama_supports_vision", lambda: True)
    monkeypatch.setattr(
        "features.llm_mention.generate_mention_result",
        lambda *args, **kwargs: None,
    )

    asyncio.run(feature._process_job(job))

    assert "couldn't process that image" in feature._reply_job_error.await_args.args[1]


def test_summon_never_consolidates_memory(monkeypatch):
    feature = object.__new__(LLMMentionFeature)
    feature.memory = SimpleNamespace(
        can_process_batch=MagicMock(return_value=True)
    )
    feature._reply_mention = AsyncMock()
    feature._enqueue_memory = AsyncMock()
    batch = MemoryBatch(
        scope_id=100,
        user_id=123,
        guild_id=100,
        request_channel_id=10,
        generation=0,
        through_sequence=1,
        observations=("hello",),
    )
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Robeeque"),
        question="",
        model="discord-bot",
        summon_only=True,
        memory_batch=batch,
    )
    monkeypatch.setattr(
        "features.llm_mention.generate_summon_reply",
        lambda *args, **kwargs: "You rang?",
    )
    asyncio.run(feature._process_job(job))

    feature._reply_mention.assert_awaited_once()
    feature._enqueue_memory.assert_not_awaited()


def test_invalidated_batch_is_not_sent_for_consolidation(monkeypatch):
    feature = object.__new__(LLMMentionFeature)
    feature.memory = SimpleNamespace(
        can_process_batch=MagicMock(return_value=False)
    )
    feature._reply_mention = AsyncMock()
    feature._enqueue_memory = AsyncMock()
    batch = MemoryBatch(
        scope_id=100,
        user_id=123,
        guild_id=100,
        request_channel_id=10,
        generation=0,
        through_sequence=1,
        observations=("sensitive stale snapshot",),
    )
    job = AskJob(
        user=SimpleNamespace(id=123, display_name="Robeeque"),
        question="hello",
        model="discord-bot",
        memory_batch=batch,
    )
    monkeypatch.setattr(
        "features.llm_mention.generate_mention_result",
        lambda *args, **kwargs: SimpleNamespace(text="Hi", reaction=None),
    )
    asyncio.run(feature._process_job(job))

    feature._enqueue_memory.assert_not_awaited()


def test_split_discord_messages_splits_long_text():
    text = "word " * 800
    chunks = split_discord_messages(text)
    assert len(chunks) > 1
    assert all(len(c) <= DISCORD_SAFE_LIMIT for c in chunks)
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")


def test_split_discord_messages_includes_prefix_in_first_chunk_only():
    text = "x" * 5000
    prefix = "**llama3.2:3b**\n"
    chunks = split_discord_messages(text, first_prefix=prefix)
    assert chunks[0].startswith(prefix)
    assert all(len(c) <= DISCORD_SAFE_LIMIT for c in chunks)
    assert prefix not in "".join(chunks[1:])
    assert len("".join(chunks)) == len(prefix) + len(text)


def test_split_discord_messages_respects_limit_with_prefix():
    text = "a" * DISCORD_MESSAGE_LIMIT
    prefix = "**model**\n"
    chunks = split_discord_messages(text, first_prefix=prefix)
    assert all(len(c) <= DISCORD_SAFE_LIMIT for c in chunks)
    assert len(chunks) > 1


def test_query_llm_rejects_unknown_model():
    with pytest.raises(LlamaCppError, match="not allowed"):
        query_llm("hi", model="unknown-model")


def test_query_llm_success(monkeypatch):
    mock_response = MagicMock()
    mock_response.ok = True
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "choices": [{"message": {"content": "Hello from llama.cpp"}}]
    }
    mock_post = MagicMock(return_value=mock_response)
    monkeypatch.setattr(requests, "post", mock_post)

    answer = query_llm(
        "hi", model=get_default_model(), base_url="http://llama-server:8080"
    )

    assert answer == "Hello from llama.cpp"
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == "http://llama-server:8080/v1/chat/completions"
    assert kwargs["json"]["model"] == get_default_model()
    assert kwargs["json"]["messages"] == [{"role": "user", "content": "hi"}]
    assert all(message["role"] != "system" for message in kwargs["json"]["messages"])
    assert kwargs["json"]["stream"] is False


def test_query_llm_server_error(monkeypatch):
    mock_response = MagicMock()
    mock_response.ok = False
    mock_response.status_code = 404
    mock_response.text = "model not found"
    mock_response.reason = "Not Found"
    monkeypatch.setattr(requests, "post", MagicMock(return_value=mock_response))

    with pytest.raises(LlamaCppError, match="HTTP 404"):
        query_llm(
            "hi", model=get_default_model(), base_url="http://llama-server:8080"
        )
