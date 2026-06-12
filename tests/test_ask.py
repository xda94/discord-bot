from unittest.mock import MagicMock

import pytest
import requests

from features.ask import (
    DEFAULT_MODEL,
    _chunk_message,
    query_ollama,
    OllamaError,
)


def test_chunk_message_splits_long_text():
    text = "line\n" * 500
    chunks = _chunk_message(text, limit=100)
    assert len(chunks) > 1
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


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
