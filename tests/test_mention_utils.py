from unittest.mock import MagicMock

import pytest

from mention_utils import extract_mention_text, get_bot_id, resolve_bot_display_name


def test_get_bot_id_from_env(monkeypatch):
    monkeypatch.setenv("BOT_ID", "123456789")
    assert get_bot_id() == 123456789


def test_resolve_bot_display_name_uses_server_nickname():
    guild = MagicMock()
    guild.me.display_name = "SkippyOnServer"
    client = MagicMock()
    client.user.display_name = "SkippyGlobal"
    assert resolve_bot_display_name(guild, client) == "SkippyOnServer"


def test_resolve_bot_display_name_falls_back_to_global_without_guild():
    client = MagicMock()
    client.user.display_name = "SkippyGlobal"
    assert resolve_bot_display_name(None, client) == "SkippyGlobal"


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
        role_mentions = []
        channel_mentions = []

    assert extract_mention_text(FakeMessage(), 99) == ""


def test_extract_mention_text_with_question():
    class FakeUser:
        id = 99

    class FakeMessage:
        mentions = [FakeUser()]
        content = "<@!99> what is python?"
        role_mentions = []
        channel_mentions = []

    assert extract_mention_text(FakeMessage(), 99) == "what is python?"


def test_extract_mention_text_resolves_other_user_mentions():
    class Bot:
        id = 99

    class Carol:
        id = 456
        display_name = "Carol"

    class FakeMessage:
        mentions = [Bot(), Carol()]
        content = "<@99> what did <@456> say?"
        role_mentions = []
        channel_mentions = []

    # The bot's own mention is stripped; other users become readable names,
    # so no raw <@id> reaches the LLM.
    assert extract_mention_text(FakeMessage(), 99) == "what did @Carol say?"
