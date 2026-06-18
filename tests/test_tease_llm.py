from unittest.mock import MagicMock

import pytest

from ollama_client import OllamaError
from tease_llm import (
    build_mention_messages,
    build_summon_prompt,
    build_tease_prompt,
    enhance_tease,
    generate_mention_reply,
    generate_summon_reply,
    normalize_tease_response,
)


def test_build_tease_prompt_includes_mood_and_context():
    prompt = build_tease_prompt("bad", "Alice", "hello there")
    assert "bad" in prompt
    assert "Alice" in prompt
    assert "hello there" in prompt
    assert "sarcastic" in prompt


def test_build_summon_prompt_includes_username():
    prompt = build_summon_prompt("Alice")
    assert "Alice" in prompt
    assert "pinged" in prompt.lower()


def test_normalize_tease_response_trims_and_strips_quotes():
    assert normalize_tease_response('  "hello"  ') == "hello"


def test_normalize_tease_response_truncates_long_text():
    text = "word " * 100
    assert len(normalize_tease_response(text)) <= 280


def test_enhance_tease_uses_llm(monkeypatch):
    monkeypatch.setattr(
        "tease_llm.query_ollama",
        lambda prompt, **kwargs: "sure buddy, riveting stuff",
    )
    result = enhance_tease("bad", "Alice", "hello")
    assert result == "sure buddy, riveting stuff"


def test_enhance_tease_returns_none_on_error(monkeypatch):
    def _fail(*args, **kwargs):
        raise OllamaError("down")

    monkeypatch.setattr("tease_llm.query_ollama", _fail)
    assert enhance_tease("bad", "Alice", "hello") is None


def test_enhance_tease_disabled(monkeypatch):
    monkeypatch.setattr("tease_llm.TEASE_LLM_ENABLED", False)
    monkeypatch.setattr(
        "tease_llm.query_ollama",
        MagicMock(side_effect=AssertionError("should not call ollama")),
    )
    assert enhance_tease("bad", "Alice", "hello") is None


def test_build_mention_messages_with_history():
    messages = build_mention_messages("Alice", "what is python?", ["Bob: hello"])
    assert messages[0]["role"] == "system"
    assert "Balen" in messages[0]["content"]
    assert messages[-1]["role"] == "user"
    user_content = messages[-1]["content"]
    assert "Bob: hello" in user_content
    assert "Alice: what is python?" in user_content
    # The triggering message must be the only real user turn, so the model
    # replies instead of continuing the multi-party transcript.
    assert sum(m["role"] == "user" for m in messages) == 1


def test_build_mention_messages_without_history():
    messages = build_mention_messages("Alice", "what is python?")
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == "Alice: what is python?"


def test_generate_mention_reply(monkeypatch):
    monkeypatch.setattr(
        "tease_llm.chat_ollama",
        lambda messages, **kwargs: "Python is a programming language.",
    )
    assert generate_mention_reply("Alice", "what is python?") == (
        "Python is a programming language."
    )


def test_generate_summon_reply(monkeypatch):
    monkeypatch.setattr(
        "tease_llm.query_ollama",
        lambda prompt, **kwargs: "You rang? What do you need?",
    )
    assert generate_summon_reply("Alice") == "You rang? What do you need?"
