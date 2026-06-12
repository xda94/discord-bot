from unittest.mock import MagicMock

import pytest

from ollama_client import OllamaError
from tease_llm import (
    build_tease_enhance_prompt,
    enhance_tease,
    normalize_tease_response,
)


def test_build_tease_enhance_prompt_includes_mood_and_line():
    prompt = build_tease_enhance_prompt("bad", "cool story, Alice")
    assert "bad" in prompt
    assert "cool story, Alice" in prompt
    assert "sarcastic" in prompt


def test_normalize_tease_response_keeps_username():
    result = normalize_tease_response(
        "wow Alice that was something",
        fallback="cool story, Alice",
        username="Alice",
    )
    assert result == "wow Alice that was something"


def test_normalize_tease_response_falls_back_without_username():
    result = normalize_tease_response(
        "wow that was something",
        fallback="cool story, Alice",
        username="Alice",
    )
    assert result == "cool story, Alice"


def test_enhance_tease_uses_llm(monkeypatch):
    monkeypatch.setattr(
        "tease_llm.query_ollama",
        lambda prompt, **kwargs: "sure buddy, Alice, riveting stuff",
    )
    result = enhance_tease("bad", "cool story, Alice", username="Alice")
    assert "Alice" in result


def test_enhance_tease_falls_back_on_error(monkeypatch):
    def _fail(*args, **kwargs):
        raise OllamaError("down")

    monkeypatch.setattr("tease_llm.query_ollama", _fail)
    original = "cool story, Alice"
    assert enhance_tease("bad", original, username="Alice") == original


def test_enhance_tease_disabled(monkeypatch):
    monkeypatch.setattr("tease_llm.TEASE_LLM_ENABLED", False)
    monkeypatch.setattr(
        "tease_llm.query_ollama",
        MagicMock(side_effect=AssertionError("should not call ollama")),
    )
    original = "cool story, Alice"
    assert enhance_tease("bad", original, username="Alice") == original
