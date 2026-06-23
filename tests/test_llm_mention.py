from unittest.mock import MagicMock

import pytest
import requests

from ollama_client import OllamaError, get_default_model, query_ollama
from features.llm_mention import (
    DISCORD_MESSAGE_LIMIT,
    DISCORD_SAFE_LIMIT,
    get_ask_cooldown_seconds,
    requests_ahead,
    split_discord_messages,
)


def test_get_ask_cooldown_seconds(monkeypatch):
    monkeypatch.setenv("ASK_COOLDOWN_SECONDS", "90")
    assert get_ask_cooldown_seconds() == 90.0


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


def test_query_ollama_rejects_unknown_model():
    with pytest.raises(OllamaError, match="not allowed"):
        query_ollama("hi", model="unknown-model")


def test_query_ollama_success(monkeypatch):
    mock_response = MagicMock()
    mock_response.ok = True
    mock_response.status_code = 200
    mock_response.json.return_value = {"response": "Hello from Ollama"}
    mock_post = MagicMock(return_value=mock_response)
    monkeypatch.setattr(requests, "post", mock_post)

    answer = query_ollama("hi", model=get_default_model(), base_url="http://ollama:11434")

    assert answer == "Hello from Ollama"
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == "http://ollama:11434/api/generate"
    assert kwargs["json"]["model"] == get_default_model()
    assert kwargs["json"]["prompt"] == "hi"
    from ollama_client import get_keep_alive
    assert kwargs["json"]["keep_alive"] == get_keep_alive()
    assert kwargs["json"]["stream"] is False


def test_query_ollama_model_not_found(monkeypatch):
    mock_response = MagicMock()
    mock_response.ok = False
    mock_response.status_code = 404
    mock_response.text = "model not found"
    mock_response.reason = "Not Found"
    monkeypatch.setattr(requests, "post", MagicMock(return_value=mock_response))

    with pytest.raises(OllamaError, match="not available"):
        query_ollama("hi", model=get_default_model(), base_url="http://ollama:11434")
