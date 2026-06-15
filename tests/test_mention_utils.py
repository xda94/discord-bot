import pytest

from mention_utils import extract_mention_text, get_bot_id


def test_get_bot_id_from_env(monkeypatch):
    monkeypatch.setenv("BOT_ID", "123456789")
    assert get_bot_id() == 123456789


def test_extract_mention_text_returns_none_without_mention():
    class FakeUser:
        id = 99

    class FakeMessage:
        mentions = []
        content = "hello"

    assert extract_mention_text(FakeMessage(), 99) is None


def test_extract_mention_text_empty_ping():
    class FakeUser:
        id = 99

    class FakeMessage:
        mentions = [FakeUser()]
        content = "<@99>"

    assert extract_mention_text(FakeMessage(), 99) == ""


def test_extract_mention_text_with_question():
    class FakeUser:
        id = 99

    class FakeMessage:
        mentions = [FakeUser()]
        content = "<@!99> what is python?"

    assert extract_mention_text(FakeMessage(), 99) == "what is python?"
