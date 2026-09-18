from unittest.mock import MagicMock

import pytest
import requests

from llm_client import (
    LlamaCppError,
    get_allowed_models,
    get_default_model,
    get_mention_model,
    llama_supports_vision,
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
        "max_tokens": 384,
        "temperature": 0.8,
    }
    assert mock_post.call_args.args[0] == "http://localhost:8080/v1/chat/completions"


@pytest.mark.parametrize("options,expected", [(None, 384), ({"format": "json"}, 384), ({"max_tokens": 32}, 32)])
def test_all_generation_paths_have_a_finite_output_budget(monkeypatch, options, expected):
    response = MagicMock(ok=True)
    response.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
    post = MagicMock(return_value=response)
    monkeypatch.setattr(requests, "post", post)

    query_llm("hello", options=options)

    assert post.call_args.kwargs["json"]["max_tokens"] == expected


@pytest.mark.parametrize("limit", [None, -1, 0, True, "100"])
def test_unbounded_or_invalid_generation_is_rejected_before_http(monkeypatch, limit):
    post = MagicMock()
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(LlamaCppError, match="positive integer"):
        query_llm("hello", options={"max_tokens": limit})
    post.assert_not_called()


def test_truncated_valid_json_is_not_returned_for_memory_commit(monkeypatch):
    response = MagicMock(ok=True)
    response.json.return_value = {"choices": [{
        "message": {"content": '{"add":[],"correct":[]}'},
        "finish_reason": "length",
    }]}
    monkeypatch.setattr(requests, "post", MagicMock(return_value=response))
    with pytest.raises(LlamaCppError, match="output token budget"):
        query_llm("extract", options={"format": "json"})


def test_timeout_logs_request_duration_without_prompt(monkeypatch, caplog):
    caplog.set_level("INFO", logger="discord_bot")
    monkeypatch.setattr(requests, "post", MagicMock(side_effect=requests.exceptions.Timeout))
    with pytest.raises(LlamaCppError, match="within 5s"):
        query_llm("private-message-content", timeout=5)
    assert "LLM request started" in caplog.text
    assert "LLM HTTP wait ended" in caplog.text
    assert "elapsed=" in caplog.text
    assert "private-message-content" not in caplog.text


def test_success_logs_server_usage_and_timings_without_content(monkeypatch, caplog):
    caplog.set_level("INFO", logger="discord_bot")
    response = MagicMock(ok=True, status_code=200, text="private-answer")
    response.json.return_value = {
        "choices": [
            {"message": {"content": "private-answer"}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 14,
            "total_tokens": 134,
            "prompt_tokens_details": {"cached_tokens": 64},
        },
        "timings": {
            "cache_n": 64,
            "prompt_ms": 250.5,
            "predicted_ms": 500.25,
            "prompt_per_second": 40.0,
            "predicted_per_second": 28.0,
        },
    }
    monkeypatch.setattr(requests, "post", MagicMock(return_value=response))

    assert query_llm("private-prompt") == "private-answer"

    assert "prompt_tokens=120" in caplog.text
    assert "cached_tokens=64" in caplog.text
    assert "completion_tokens=14" in caplog.text
    assert "prompt_ms=250.5" in caplog.text
    assert "predicted_tps=28.0" in caplog.text
    assert "private-prompt" not in caplog.text
    assert "private-answer" not in caplog.text


def test_query_llm_sends_multimodal_image_content(monkeypatch):
    response = MagicMock()
    response.ok = True
    response.json.return_value = {
        "choices": [{"message": {"content": "A small PNG."}}]
    }
    mock_post = MagicMock(return_value=response)
    monkeypatch.setattr(requests, "post", mock_post)

    result = query_llm(
        "Describe it.",
        image_bytes=b"\x89PNG",
        image_mime="image/png",
    )

    assert result == "A small PNG."
    content = mock_post.call_args.kwargs["json"]["messages"][0]["content"]
    assert content == [
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,iVBORw=="},
        },
        {"type": "text", "text": "Describe it."},
    ]


def test_query_llm_requires_complete_supported_image_input():
    with pytest.raises(LlamaCppError, match="provided together"):
        query_llm("Describe it.", image_bytes=b"png")
    with pytest.raises(LlamaCppError, match="Unsupported image MIME"):
        query_llm(
            "Describe it.", image_bytes=b"webp", image_mime="image/webp"
        )


@pytest.mark.parametrize(("vision", "expected"), [(True, True), (False, False)])
def test_llama_supports_vision_reads_server_props(monkeypatch, vision, expected):
    response = MagicMock()
    response.ok = True
    response.json.return_value = {"modalities": {"vision": vision}}
    mock_get = MagicMock(return_value=response)
    monkeypatch.setattr(requests, "get", mock_get)

    assert llama_supports_vision(base_url="http://localhost:8080/v1") is expected
    assert mock_get.call_args.args[0] == "http://localhost:8080/props"


def test_llama_supports_vision_rejects_invalid_props(monkeypatch):
    response = MagicMock()
    response.ok = True
    response.json.return_value = {"modalities": {}}
    monkeypatch.setattr(requests, "get", MagicMock(return_value=response))

    with pytest.raises(LlamaCppError, match="invalid vision-capability"):
        llama_supports_vision()


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
