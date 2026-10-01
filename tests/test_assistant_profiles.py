import sqlite3

import db
import pytest

from assistant_profiles import DEFAULT_PROFILE, effective_profile, validate_profile_updates
from llm.responses import build_mention_prompt, build_summon_prompt


def test_profile_read_does_not_create_and_defaults_are_effective(tmp_db):
    assert db.get_assistant_profile(123) is None
    with sqlite3.connect(tmp_db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM assistant_profiles").fetchone()[0] == 0
    assert effective_profile(None) == DEFAULT_PROFILE


def test_partial_profile_updates_preserve_existing_fields(tmp_db):
    first = db.set_assistant_profile(123, language="ro", timezone="Europe/Bucharest")
    second = db.set_assistant_profile(123, tone="friendly")
    assert first["language"] == "ro"
    assert second == {
        "language": "ro",
        "tone": "friendly",
        "currency": None,
        "timezone": "Europe/Bucharest",
        "notification_style": "standard",
        "llm_behavior": "balanced",
    }


def test_profile_reset_restores_effective_defaults(tmp_db):
    db.set_assistant_profile(123, currency="RON", llm_behavior="detailed")
    assert db.reset_assistant_profile(123) is True
    assert db.get_assistant_profile(123) is None
    assert effective_profile(None) == DEFAULT_PROFILE


def test_profile_validation_rejects_bad_timezone_and_currency():
    with pytest.raises(ValueError, match="IANA"):
        validate_profile_updates(timezone="Mars/Olympus")
    with pytest.raises(ValueError, match="Currency"):
        validate_profile_updates(currency="BTC")


def test_database_helper_rejects_invalid_profile_values(tmp_db):
    assert db.set_assistant_profile(123, timezone="Mars/Olympus") is None
    assert db.set_assistant_profile(123, language="de") is None
    assert db.get_assistant_profile(123) is None


def test_profile_prompt_defaults_and_explicit_language_precedence():
    default_prompt = build_mention_prompt("Ana", "Salut")
    assert "use <current_message>'s language" in default_prompt

    profile = effective_profile({
        "language": "ro", "tone": "formal", "currency": None,
        "timezone": "UTC", "notification_style": "standard",
        "llm_behavior": "concise",
    })
    prompt = build_mention_prompt("Ana", "Answer in English", assistant_profile=profile)
    assert "use Romanian unless <current_message> explicitly requests another language" in prompt
    assert "professional, formal" in prompt
    assert "be concise" in prompt
    assert "unless the requester explicitly asks for another language" in build_summon_prompt(
        "Ana", assistant_profile=profile
    )
