from unittest.mock import MagicMock

import pytest
import requests

from features.ask import (
    DEFAULT_MODEL,
    DISCORD_MESSAGE_LIMIT,
    DISCORD_SAFE_LIMIT,
    format_ask_messages,
    split_discord_messages,
    query_ollama,
    OllamaError,
)


def test_format_ask_messages_shows_question():
    messages = format_ask_messages("llama3.2:3b", "What is Python?", "A programming language.")
    assert messages[0].startswith("**llama3.2:3b**\n**Q:** What is Python?\n\n")
    assert "A programming language." in messages[0]


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

    answer = query_ollama("hi", model=DEFAULT_MODEL, base_url="http://ollama:11434")

    assert answer == "Hello from Ollama"
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == "http://ollama:11434/api/generate"
    assert kwargs["json"]["model"] == DEFAULT_MODEL
    assert kwargs["json"]["prompt"] == "hi"
    assert kwargs["json"]["keep_alive"] == 0
    assert kwargs["json"]["stream"] is False


def test_query_ollama_model_not_found(monkeypatch):
    mock_response = MagicMock()
    mock_response.ok = False
    mock_response.status_code = 404
    mock_response.text = "model not found"
    mock_response.reason = "Not Found"
    monkeypatch.setattr(requests, "post", MagicMock(return_value=mock_response))

    with pytest.raises(OllamaError, match="not available"):
        query_ollama("hi", model=DEFAULT_MODEL, base_url="http://ollama:11434")
