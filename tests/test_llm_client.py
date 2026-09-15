from unittest.mock import MagicMock

import pytest
import requests

from llm_client import (
    LlamaCppError,
    get_allowed_models,
    get_default_model,
    get_mention_model,
    query_llm,
)


def test_get_allowed_models_from_env(monkeypatch):
    monkeypatch.setenv(
        "LLAMA_CPP_ALLOWED_MODELS",
        "model-a, model-b ,model-c",
    )
    assert get_allowed_models() == ("model-a", "model-b", "model-c")


def test_get_default_model_from_env(monkeypatch):
    monkeypatch.setenv("LLAMA_CPP_ALLOWED_MODELS", "alpha,beta")
    monkeypatch.setenv("LLAMA_CPP_DEFAULT_MODEL", "beta")
    assert get_default_model() == "beta"


def test_get_default_model_not_in_allowed_raises(monkeypatch):
    monkeypatch.setenv("LLAMA_CPP_ALLOWED_MODELS", "alpha,beta")
    monkeypatch.setenv("LLAMA_CPP_DEFAULT_MODEL", "missing")
    with pytest.raises(LlamaCppError, match="not listed"):
        get_default_model()


def test_get_allowed_models_missing_raises(monkeypatch):
    monkeypatch.delenv("LLAMA_CPP_ALLOWED_MODELS", raising=False)
    with pytest.raises(LlamaCppError, match="not set"):
        get_allowed_models()


def test_get_allowed_models_empty_raises(monkeypatch):
    monkeypatch.setenv("LLAMA_CPP_ALLOWED_MODELS", "  ,  ")
    with pytest.raises(LlamaCppError, match="empty"):
        get_allowed_models()


def test_get_mention_model_from_env(monkeypatch):
    monkeypatch.setenv("LLAMA_CPP_ALLOWED_MODELS", "alpha,beta,gamma")
    monkeypatch.setenv("LLAMA_CPP_DEFAULT_MODEL", "alpha")
    monkeypatch.setenv("MENTION_LLAMA_CPP_MODEL", "gamma")
    assert get_mention_model() == "gamma"


def test_get_mention_model_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("LLAMA_CPP_ALLOWED_MODELS", "alpha,beta")
    monkeypatch.setenv("LLAMA_CPP_DEFAULT_MODEL", "beta")
    monkeypatch.delenv("MENTION_LLAMA_CPP_MODEL", raising=False)
    assert get_mention_model() == "beta"


def test_get_mention_model_not_in_allowed_raises(monkeypatch):
    monkeypatch.setenv("LLAMA_CPP_ALLOWED_MODELS", "alpha,beta")
    monkeypatch.setenv("LLAMA_CPP_DEFAULT_MODEL", "alpha")
    monkeypatch.setenv("MENTION_LLAMA_CPP_MODEL", "missing")
    with pytest.raises(LlamaCppError, match="MENTION_LLAMA_CPP_MODEL"):
        get_mention_model()


def test_get_default_model_missing_raises(monkeypatch):
    monkeypatch.setenv("LLAMA_CPP_ALLOWED_MODELS", "alpha")
    monkeypatch.delenv("LLAMA_CPP_DEFAULT_MODEL", raising=False)
    with pytest.raises(LlamaCppError, match="not set"):
        get_default_model()


def test_query_llm_sends_chat_completion_options(monkeypatch):
    response = MagicMock()
    response.ok = True
    response.json.return_value = {
        "choices": [{"message": {"content": "test response"}}]
    }
    mock_post = MagicMock(return_value=response)
    monkeypatch.setattr(requests, "post", mock_post)

    result = query_llm(
        "hello",
        options={"temperature": 0.8},
        base_url="http://localhost:8080/v1",
    )

    assert result == "test response"
    _, kwargs = mock_post.call_args
    assert kwargs["json"] == {
        "model": "discord-bot",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
        "temperature": 0.8,
    }
    assert mock_post.call_args.args[0] == "http://localhost:8080/v1/chat/completions"


def test_query_llm_translates_json_output_format(monkeypatch):
    response = MagicMock()
    response.ok = True
    response.json.return_value = {
        "choices": [{"message": {"content": "{}"}}]
    }
    mock_post = MagicMock(return_value=response)
    monkeypatch.setattr(requests, "post", mock_post)

    query_llm("extract", options={"format": "json", "temperature": 0.0})

    payload = mock_post.call_args.kwargs["json"]
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["temperature"] == 0.0
    assert "format" not in payload


def test_query_llm_sends_json_response_schema(monkeypatch):
    response = MagicMock()
    response.ok = True
    response.json.return_value = {
        "choices": [{"message": {"content": '{"memory": null}'}}]
    }
    mock_post = MagicMock(return_value=response)
    monkeypatch.setattr(requests, "post", mock_post)
    schema = {
        "type": "object",
        "properties": {"memory": {"type": "string"}},
        "required": ["memory"],
    }

    query_llm(
        "extract",
        options={"format": "json", "temperature": 0.0},
        response_schema=schema,
    )

    payload = mock_post.call_args.kwargs["json"]
    assert payload["response_format"] == {
        "type": "json_object",
        "schema": schema,
    }
    assert "response_schema" not in payload


def test_query_llm_sends_optional_api_key(monkeypatch):
    monkeypatch.setenv("LLAMA_CPP_API_KEY", "secret")
    response = MagicMock()
    response.ok = True
    response.json.return_value = {
        "choices": [{"message": {"content": "ok"}}]
    }
    mock_post = MagicMock(return_value=response)
    monkeypatch.setattr(requests, "post", mock_post)

    query_llm("hello")

    assert mock_post.call_args.kwargs["headers"]["Authorization"] == "Bearer secret"


def test_query_llm_rejects_invalid_response(monkeypatch):
    response = MagicMock()
    response.ok = True
    response.json.return_value = {"choices": []}
    monkeypatch.setattr(requests, "post", MagicMock(return_value=response))

    with pytest.raises(LlamaCppError, match="invalid chat-completion"):
        query_llm("hello")
