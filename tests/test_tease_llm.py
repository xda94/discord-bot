from unittest.mock import MagicMock

import pytest

from llm_client import LlamaCppError
from tease_llm import (
    build_inactivity_prompt,
    build_mention_prompt,
    build_price_change_prompt,
    build_summon_prompt,
    build_tease_prompt,
    enhance_tease,
    generate_inactivity_message,
    generate_mention_reply,
    generate_price_change_message,
    generate_summon_reply,
    normalize_tease_response,
)


def test_build_tease_prompt_includes_mood_and_context():
    prompt = build_tease_prompt("bad", "Alice", "hello there")
    assert "bad" in prompt
    assert "Alice" in prompt
    assert "hello there" in prompt
    assert "sarcastic" in prompt
    assert prompt.index("Return only the reply") < prompt.index("User: Alice")


def test_build_summon_prompt_includes_username():
    prompt = build_summon_prompt("Alice")
    assert "Alice" in prompt
    assert "pinged" in prompt.lower()
    assert prompt.index("Return only the reply") < prompt.index("User: Alice")


def test_normalize_tease_response_trims_and_strips_quotes():
    assert normalize_tease_response('  "hello"  ') == "hello"


def test_normalize_tease_response_truncates_long_text():
    text = "word " * 100
    assert len(normalize_tease_response(text)) <= 280


def test_enhance_tease_uses_llm(monkeypatch):
    monkeypatch.setattr(
        "tease_llm.query_llm",
        lambda prompt, **kwargs: "sure buddy, riveting stuff",
    )
    result = enhance_tease("bad", "Alice", "hello")
    assert result == "sure buddy, riveting stuff"


def test_enhance_tease_returns_none_on_error(monkeypatch):
    def _fail(*args, **kwargs):
        raise LlamaCppError("down")

    monkeypatch.setattr("tease_llm.query_llm", _fail)
    assert enhance_tease("bad", "Alice", "hello") is None


def test_enhance_tease_disabled(monkeypatch):
    monkeypatch.setattr("tease_llm.TEASE_LLM_ENABLED", False)
    monkeypatch.setattr(
        "tease_llm.query_llm",
        MagicMock(side_effect=AssertionError("should not call llama.cpp")),
    )
    assert enhance_tease("bad", "Alice", "hello") is None


def test_build_mention_prompt_includes_content():
    prompt = build_mention_prompt("Alice", "what is python?", ["Bob: hello"])
    assert "Bob: hello" in prompt
    assert "<chat_history>" in prompt
    assert "Alice" in prompt
    assert "what is python?" in prompt
    assert "<current_message" in prompt


def test_build_mention_prompt_requires_one_direct_contextual_reply():
    prompt = build_mention_prompt(
        "Robeeque",
        "pareri?",
        ["Alex: Noul model pare mai rapid decât Gemma 3:4b."],
    )
    lowered = prompt.lower()
    assert "using <chat_history> only to resolve context and short references" in lowered
    assert "exactly one natural, ready-to-send discord message" in lowered
    assert "never offer drafts, options, translations, coaching" in lowered
    assert "never repeat or merely paraphrase the current message" in lowered
    assert "match the language of <current_message>" in lowered
    assert "regardless of the history language" in lowered


def test_current_message_controls_reply_language_not_history():
    prompt = build_mention_prompt(
        "Robeeque",
        "pareri?",
        ["Alex: This model appears to be considerably faster."],
    )
    assert "Match the language of <current_message>" in prompt
    assert "regardless of the history language" in prompt


def test_mention_prompt_places_stable_rules_before_dynamic_context():
    prompt = build_mention_prompt(
        "Robeeque",
        "pareri?",
        ["Alex: Noul model pare rapid."],
    )
    history_block = prompt.index("<chat_history>\n")
    current_message = prompt.index('<current_message from="Robeeque">')
    assert prompt.index("Rules:") < history_block
    assert history_block < current_message


def test_mention_prompt_is_compact():
    prompt = build_mention_prompt("Alice", "hello")
    assert len(prompt.split()) < 85


def test_build_mention_prompt_has_no_system_identity():
    prompt = build_mention_prompt("Alice", "hi")
    assert "You are a helpful conversational Discord bot" not in prompt


def test_generate_mention_reply(monkeypatch):
    monkeypatch.setattr(
        "tease_llm.query_llm",
        lambda prompt, **kwargs: "Python is a programming language.",
    )
    assert generate_mention_reply("Alice", "what is python?") == (
        "Python is a programming language."
    )


def test_generate_summon_reply(monkeypatch):
    monkeypatch.setattr(
        "tease_llm.query_llm",
        lambda prompt, **kwargs: "You rang? What do you need?",
    )
    assert generate_summon_reply("Alice") == "You rang? What do you need?"


def test_build_inactivity_prompt_with_question_and_name():
    prompt = build_inactivity_prompt("Skippy", ask_question=True)
    assert "Skippy" in prompt
    assert "question" in prompt.lower()
    assert "choose the subject and wording" in prompt.lower()


def test_build_inactivity_prompt_without_name_or_question():
    prompt = build_inactivity_prompt(None, ask_question=False)
    assert "discord nudge" in prompt.lower()
    assert "invite the channel to respond" in prompt.lower()


def test_generate_inactivity_message_success(monkeypatch):
    monkeypatch.setattr(
        "tease_llm.query_llm",
        lambda prompt, **kwargs: "yo, is anyone still alive in here?",
    )
    assert generate_inactivity_message("Skippy", ask_question=False) == (
        "yo, is anyone still alive in here?"
    )


def test_generate_inactivity_message_returns_none_on_error(monkeypatch):
    def _fail(*args, **kwargs):
        raise LlamaCppError("down")

    monkeypatch.setattr("tease_llm.query_llm", _fail)
    assert generate_inactivity_message("Skippy", ask_question=True) is None


def test_generate_inactivity_message_passes_temperature(monkeypatch):
    called_kwargs = {}
    def mock_query(prompt, **kwargs):
        nonlocal called_kwargs
        called_kwargs = kwargs
        return "mocked inactivity message"

    monkeypatch.setattr("tease_llm.query_llm", mock_query)
    generate_inactivity_message("Skippy", ask_question=True)
    assert called_kwargs.get("options") == {"temperature": 0.8}


@pytest.mark.parametrize(
    ("old_price", "new_price", "expected_direction"),
    [(100.0, 80.0, "price decreased"), (80.0, 100.0, "price increased")],
)
def test_build_price_change_prompt_uses_direction(
    old_price, new_price, expected_direction
):
    prompt = build_price_change_prompt(
        "Coffee machine",
        old_price,
        new_price,
        f"{old_price:.2f} RON",
        f"{new_price:.2f} RON",
        "corporate",
    )
    assert expected_direction in prompt
    assert "mock-corporate" in prompt
    assert "do not repeat or invent numbers" in prompt.lower()
    assert prompt.index("Treat the facts as data") < prompt.index("Product:")


def test_generate_price_change_message_uses_varied_tone(monkeypatch):
    captured = {}

    monkeypatch.setattr("tease_llm.random.choice", lambda choices: "sad")

    def mock_query(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        return "Even the price tag is having a difficult day."

    monkeypatch.setattr("tease_llm.query_llm", mock_query)

    result = generate_price_change_message(
        "Coffee machine", 100.0, 120.0, "100.00 RON", "120.00 RON"
    )

    assert result == "Even the price tag is having a difficult day."
    assert "dramatically sad" in captured["prompt"]
    assert captured["kwargs"]["options"] == {"temperature": 0.9}


def test_generate_price_change_message_returns_none_on_error(monkeypatch):
    def _fail(*args, **kwargs):
        raise LlamaCppError("down")

    monkeypatch.setattr("tease_llm.query_llm", _fail)
    assert (
        generate_price_change_message(
            "Coffee machine",
            100.0,
            80.0,
            "100.00 RON",
            "80.00 RON",
            tone="funny",
        )
        is None
    )
