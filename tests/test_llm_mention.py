import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import requests

from llm_client import LlamaCppError, get_default_model, query_llm
from features.llm_mention import (
    AskJob,
    LLMMentionFeature,
    DISCORD_MESSAGE_LIMIT,
    DISCORD_SAFE_LIMIT,
    get_ask_cooldown_seconds,
    get_selected_model,
    requests_ahead,
    split_discord_messages,
)


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


def test_stale_stored_model_is_replaced_with_llama_cpp_default(tmp_db):
    import db

    db.set_setting("mention_model", "old-ollama-model")

    assert get_selected_model() == "discord-bot"
    assert db.get_setting("mention_model") == "discord-bot"


def test_requests_ahead():
    assert requests_ahead(processing=False, queue_size=0) == 0
    assert requests_ahead(processing=True, queue_size=0) == 1
    assert requests_ahead(processing=True, queue_size=2) == 3
    assert requests_ahead(processing=False, queue_size=2) == 2


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
