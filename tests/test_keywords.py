"""Tests for the `_pick_response` helper in `features.keywords`.

This helper exists as a standalone function precisely so it's directly
unit-testable — the previous in-place `while new_response == last_one`
loop was a notorious infinite-loop hazard when the user added the same
response twice for a keyword.
"""

import random

import pytest

from features.keywords import _pick_response


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
