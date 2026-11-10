"""Tests for the `_pick_response` helper in `features.keywords`.

This helper exists as a standalone function precisely so it's directly
unit-testable — the previous in-place `while new_response == last_one`
loop was a notorious infinite-loop hazard when the user added the same
response twice for a keyword.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

import db
from features.keywords import KeywordsFeature, _pick_response


def test_picks_an_alternative_when_available():
    """When `last_one` is in the list but alternatives exist, the result
    must never equal `last_one`. Repeat the draw to dodge a one-off
    lucky run."""
    options = ["a", "b", "c"]
    for _ in range(50):
        assert _pick_response(options, "a") != "a"


def test_returns_only_option_when_no_alternative():
    """Single-element list — the only thing to pick is that one element,
    even if it equals `last_one`."""
    assert _pick_response(["only"], "only") == "only"


def test_no_infinite_loop_when_all_options_equal_last_one():
    """Regression test for the original bug: every option equals
    `last_one`. The previous `while` loop spun forever; this version
    returns the duplicate value."""
    assert _pick_response(["dup", "dup"], "dup") == "dup"
    assert _pick_response(["dup", "dup", "dup"], "dup") == "dup"


def test_first_call_picks_any(monkeypatch):
    """`last_one` is None on the first call for a keyword — every option
    must be reachable."""
    seen = set()
    for _ in range(100):
        seen.add(_pick_response(["a", "b", "c"], None))
    assert seen == {"a", "b", "c"}


def test_distribution_uses_alternatives_uniformly(monkeypatch):
    """When alternatives exist, each one must be reachable — i.e. we don't
    accidentally always pick the same alternative."""
    seen = set()
    for _ in range(100):
        seen.add(_pick_response(["a", "b", "c"], "a"))
    assert seen == {"b", "c"}


@pytest.mark.parametrize("saved", [True, False])
def test_keyword_add_acknowledges_persistence_result(monkeypatch, saved):
    commands = {}
    tree = Mock()
    tree.command.side_effect = lambda **options: lambda callback: commands.setdefault(
        options["name"], callback
    )
    KeywordsFeature(MagicMock(), tree, MagicMock(), MagicMock())
    interaction = MagicMock()
    interaction.guild.id = 20
    interaction.response.send_message = AsyncMock()
    add_response = Mock(return_value=saved)
    monkeypatch.setattr(db, "add_response", add_response)

    asyncio.run(commands["keyword-add"](interaction, "hello", "world"))

    add_response.assert_called_once_with("hello", "world", 20)
    if saved:
        interaction.response.send_message.assert_awaited_once_with(
            "Added keyword **hello** for this server."
        )
    else:
        interaction.response.send_message.assert_awaited_once_with(
            "Failed to save this keyword. Please try again.", ephemeral=True
        )


def _keyword_feature():
    from features.response_gate import ResponseGate

    tree = Mock()
    tree.command.side_effect = lambda **options: lambda callback: callback
    sponsors = MagicMock()
    sponsors.maybe_get_sponsor_suffix.return_value = None
    return KeywordsFeature(MagicMock(), tree, ResponseGate(), sponsors)


def _keyword_message(guild_id, content):
    message = MagicMock()
    message.guild.id = guild_id
    message.author.id = 5
    message.content = content
    message.reply = AsyncMock()
    return message


@pytest.fixture
def keyword_env(monkeypatch):
    responses = {}
    monkeypatch.setattr(db, "get_cached_responses", lambda guild_id: responses.get(guild_id))
    monkeypatch.setattr(db, "log_keyword_usage", Mock())
    monkeypatch.setattr("features.keywords.record", AsyncMock())
    return responses


def test_warm_cache_without_match_does_no_thread_dispatch(keyword_env, monkeypatch):
    keyword_env[1] = {"hello": ["world"], "bye": ["later"]}
    to_thread = Mock(side_effect=AssertionError("no thread dispatch expected"))
    monkeypatch.setattr("features.keywords.asyncio.to_thread", to_thread)
    feature = _keyword_feature()
    message = _keyword_message(1, "nothing to see, helloworld")

    assert asyncio.run(feature.handle_message(message)) is False
    to_thread.assert_not_called()
    message.reply.assert_not_awaited()


def test_cache_miss_falls_back_to_database(keyword_env, monkeypatch):
    get_all = Mock(return_value={"hello": ["world"]})
    monkeypatch.setattr(db, "get_all_responses", get_all)
    feature = _keyword_feature()
    message = _keyword_message(1, "Hello there")

    assert asyncio.run(feature.handle_message(message)) is True
    get_all.assert_called_once_with(1)
    message.reply.assert_awaited_once_with("world", mention_author=False, suppress_embeds=False)


def test_first_keyword_in_dict_order_wins(keyword_env):
    keyword_env[1] = {"hi": ["short"], "hi there": ["long"]}
    feature = _keyword_feature()
    message = _keyword_message(1, "hi there friend")

    assert asyncio.run(feature.handle_message(message)) is True
    message.reply.assert_awaited_once_with("short", mention_author=False, suppress_embeds=False)


def test_keywords_match_on_word_boundaries_and_escape_specials(keyword_env):
    keyword_env[1] = {"c++": ["lang"], "cat": ["meow"]}
    feature = _keyword_feature()

    assert asyncio.run(feature.handle_message(_keyword_message(1, "concatenate"))) is False
    message = _keyword_message(1, "I like c++ a lot")
    assert asyncio.run(feature.handle_message(message)) is True
    message.reply.assert_awaited_once_with("lang", mention_author=False, suppress_embeds=False)


def test_matcher_is_rebuilt_when_responses_dict_is_replaced(keyword_env):
    keyword_env[1] = {"old": ["x"]}
    feature = _keyword_feature()
    assert asyncio.run(feature.handle_message(_keyword_message(1, "new"))) is False

    keyword_env[1] = {"new": ["y"]}
    message = _keyword_message(1, "new")
    assert asyncio.run(feature.handle_message(message)) is True
    message.reply.assert_awaited_once_with("y", mention_author=False, suppress_embeds=False)


def test_keyword_cooldown_is_per_guild(keyword_env):
    keyword_env[1] = {"ping": ["pong"]}
    keyword_env[2] = {"ping": ["pong"]}
    feature = _keyword_feature()

    second_a = _keyword_message(1, "ping")
    assert asyncio.run(feature.handle_message(_keyword_message(1, "ping"))) is True
    assert asyncio.run(feature.handle_message(_keyword_message(2, "ping"))) is True
    assert asyncio.run(feature.handle_message(second_a)) is False
    second_a.reply.assert_not_awaited()


def test_get_cached_responses_only_returns_warm_cache(tmp_db, monkeypatch):
    import db.bot_data as bot_data

    db.add_response("kw", "value", 30)
    assert db.get_cached_responses(30) is None
    loaded = db.get_all_responses(30)
    assert db.get_cached_responses(30) is loaded

    expired = bot_data._responses_cache_at[30] + bot_data._RESPONSES_CACHE_TTL
    monkeypatch.setattr("db.bot_data.time.time", lambda: expired)
    assert db.get_cached_responses(30) is None


def test_response_gate_tracks_keys_independently(monkeypatch):
    from features.response_gate import ResponseGate

    now = [1000.0]
    monkeypatch.setattr("features.response_gate.time.time", lambda: now[0])
    gate = ResponseGate()
    assert gate.can_respond(1) and gate.can_respond(2)
    gate.mark_responded(1)
    assert not gate.can_respond(1)
    assert gate.can_respond(2)
    now[0] += 10
    assert gate.can_respond(1)
