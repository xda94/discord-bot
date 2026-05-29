"""Tests for the buy-signal alert classifier in `features.scraping`.

`_classify_price` is the pure decision function the scrape loop calls
on every pass to decide whether to fire a LOW ("buy now") or HIGH
("maybe wait") DM. These tests exhaustively cover the state machine
so we won't accidentally re-introduce spammy or missed alerts on a
future refactor.
"""

import pytest

from features.scraping import (
    ALERT_LOW_REALERT_DROP_PCT,
    ALERT_MIN_DATA_POINTS,
    AlertDecision,
    _classify_price,
)


# ---------------------------------------------------------------------------
# Guardrails (insufficient data)
# ---------------------------------------------------------------------------

def test_no_alert_when_current_is_none():
    decision = _classify_price(None, [10.0] * 20, None, None)
    assert decision.alert_kind is None


def test_no_alert_when_history_below_minimum():
    short_history = [10.0] * (ALERT_MIN_DATA_POINTS - 1)
    decision = _classify_price(5.0, short_history, None, None)
    assert decision.alert_kind is None
    # State must also stay where it was — we made no observation.
    assert decision.new_state == None  # noqa: E711  (explicit None for clarity)


def test_no_alert_when_history_has_too_few_numeric_values():
    """`None` entries (e.g. price-less scrapes from older rows) must be
    filtered before the minimum-points check."""
    history = [None, None, 10.0, 10.0, 10.0]  # only 3 numeric
    decision = _classify_price(5.0, history, None, None)
    assert decision.alert_kind is None


def test_filters_non_numeric_history_entries():
    """Defensive: a malformed row in history shouldn't crash the classifier."""
    history = [10.0, 11.0, "garbage", 9.0, 10.0, 11.0, 10.0, 9.0]
    # 7 numeric values; should still classify cleanly.
    decision = _classify_price(8.0, history, None, None)
    assert decision.alert_kind == "low"


# ---------------------------------------------------------------------------
# LOW zone — initial entry
# ---------------------------------------------------------------------------

def _enough(history_floor=10.0):
    """7 prices at the given floor, used as a stable baseline."""
    return [history_floor] * ALERT_MIN_DATA_POINTS


def test_low_fires_on_first_entry_below_history_min():
    history = [12.0, 11.0, 13.0, 12.0, 11.0, 12.0, 11.0]  # min = 11
    decision = _classify_price(10.0, history, last_alert_kind=None, last_alert_price=None)
    assert decision.alert_kind == "low"
    assert decision.new_state == "low"
    assert decision.new_state_price == 10.0
    assert decision.all_time_low == 11.0


def test_low_fires_when_current_matches_existing_low_with_real_variance():
    """`current <= all_time_low` is inclusive — a price that returns to
    a historical floor (after having moved away from it) still triggers
    a LOW alert. The variance guard does NOT suppress this case because
    the history actually shows movement (max meaningfully above min)."""
    # min=10, max=12 — 20% spread, well above the 1% variance floor.
    history = [10.0, 12.0, 11.0, 12.0, 10.0, 11.0, 12.0]
    decision = _classify_price(10.0, history, None, None)
    assert decision.alert_kind == "low"
    assert decision.new_state == "low"


def test_no_alert_when_history_is_essentially_flat():
    """Variance guard: a perfectly flat history (max == min) yields no
    meaningful zone signal. Must not fire LOW on first crossing of the
    minimum-data-points threshold (the stable-item bootstrap bug).

    The guard kicks in whenever the spread is below the LOW re-alert
    threshold (~1 %), so sub-percent noise also doesn't trigger."""
    history = [164.78] * 7  # perfectly flat — the real-world Piper Heidsieck case
    decision = _classify_price(164.78, history, None, None)
    assert decision.alert_kind is None
    # State stays neutral too — we're not in any zone we can reason about.
    assert decision.new_state is None


def test_no_alert_when_history_spread_below_variance_threshold():
    """Spread is non-zero but below the 1 % floor → still considered flat."""
    history = [100.0, 100.5, 100.3, 100.2, 100.4, 100.1, 100.5]
    # max(100.5) < min(100.0) * 1.01 = 101.0 → flat
    decision = _classify_price(100.0, history, None, None)
    assert decision.alert_kind is None


def test_variance_check_unlocks_once_price_moves_meaningfully():
    """Once a single scrape returns a price ≥1 % away from the flat
    floor, the variance guard releases and normal LOW/HIGH logic resumes
    on the very next pass."""
    # First 7 readings flat at 100, then one big drop to 90 → history now
    # has real spread (max=100, min=90, well over 1 %).
    history = [100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 90.0]
    decision = _classify_price(90.0, history, None, None)
    assert decision.alert_kind == "low"


def test_low_fires_when_transitioning_from_high():
    history = [10.0, 11.0, 12.0, 10.0, 11.0, 12.0, 10.0]  # min = 10
    decision = _classify_price(
        9.0, history,
        last_alert_kind="high",
        last_alert_price=20.0,
    )
    assert decision.alert_kind == "low"
    assert decision.new_state == "low"
    assert decision.new_state_price == 9.0


# ---------------------------------------------------------------------------
# LOW zone — re-alert behaviour
# ---------------------------------------------------------------------------

def test_low_does_not_realert_at_same_price():
    """Already in low at $10, scrape returns $10 again — no DM, state
    intact. This is the case that would spam every 12h without the
    re-alert threshold."""
    history = [10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0]
    decision = _classify_price(
        10.0, history,
        last_alert_kind="low",
        last_alert_price=10.0,
    )
    assert decision.alert_kind is None
    assert decision.new_state == "low"
    # Price-at-alert is preserved when we don't realert.
    assert decision.new_state_price == 10.0


def test_low_does_not_realert_for_negligible_drop():
    """0.5 % below previous alert is below the 1 % threshold — no DM."""
    history = [10.0] * 7
    new_price = 10.0 * (1 - ALERT_LOW_REALERT_DROP_PCT / 2)  # 0.5% below
    decision = _classify_price(
        new_price, history,
        last_alert_kind="low",
        last_alert_price=10.0,
    )
    assert decision.alert_kind is None
    # State remains low; remembered price unchanged.
    assert decision.new_state == "low"
    assert decision.new_state_price == 10.0


def test_low_realerts_on_meaningfully_lower_price():
    """≥1 % below the previous LOW alert — fire again so the user knows
    the floor has dropped further."""
    history = [10.0] * 7
    new_price = 10.0 * (1 - ALERT_LOW_REALERT_DROP_PCT * 2)  # 2% below
    decision = _classify_price(
        new_price, history,
        last_alert_kind="low",
        last_alert_price=10.0,
    )
    assert decision.alert_kind == "low"
    assert decision.new_state == "low"
    # Remembered price is updated to the new floor for the next comparison.
    assert decision.new_state_price == new_price


# ---------------------------------------------------------------------------
# HIGH zone — initial entry + no-realert
# ---------------------------------------------------------------------------

def test_high_fires_on_first_entry_above_median():
    history = [10.0, 11.0, 10.0, 11.0, 10.0, 11.0, 10.0]  # median = 10
    decision = _classify_price(15.0, history, None, None)
    assert decision.alert_kind == "high"
    assert decision.new_state == "high"
    assert decision.new_state_price == 15.0
    assert decision.median_price == 10.0


def test_high_does_not_fire_at_exactly_median():
    """`current > median` is strict — equality is neutral, not high."""
    history = [10.0] * 7
    decision = _classify_price(10.0, history, None, None)
    # Equals median, equals all_time_low → zone is "low" by the inclusive
    # rule, not "high". (Stable-item edge case we accept.)
    assert decision.alert_kind != "high"


def test_high_does_not_realert_while_still_high():
    """Already in high zone, another above-median scrape → no DM."""
    history = [10.0, 11.0, 10.0, 11.0, 10.0, 11.0, 10.0]  # median = 10
    decision = _classify_price(
        16.0, history,
        last_alert_kind="high",
        last_alert_price=15.0,
    )
    assert decision.alert_kind is None
    assert decision.new_state == "high"
    # The remembered price stays at the original alert — we don't track
    # the running max.
    assert decision.new_state_price == 15.0


def test_high_rearms_after_returning_to_neutral():
    """Was high, dropped back to median, then climbed above median again:
    fires a second HIGH because the state was cleared in between."""
    history = [8.0, 10.0, 12.0, 9.0, 11.0, 10.0, 12.0]  # min=8, median=10

    # Pass 1: was high (price 15), drops to median → neutral, state clears.
    pass1 = _classify_price(10.0, history, last_alert_kind="high", last_alert_price=15.0)
    assert pass1.alert_kind is None
    assert pass1.new_state is None  # neutral
    assert pass1.new_state_price is None

    # Pass 2: climbs above median again, with cleared state → fires.
    pass2 = _classify_price(
        15.0, history,
        last_alert_kind=pass1.new_state,
        last_alert_price=pass1.new_state_price,
    )
    assert pass2.alert_kind == "high"
    assert pass2.new_state == "high"


# ---------------------------------------------------------------------------
# Neutral zone — no alerts, state hygiene
# ---------------------------------------------------------------------------

def test_neutral_clears_existing_low_state():
    """Was at the all-time low, price climbs back into the middle:
    no DM, but state clears so the next LOW re-entry fires fresh."""
    history = [8.0, 10.0, 12.0, 9.0, 11.0, 10.0, 12.0]  # min=8, median=10
    decision = _classify_price(
        9.0, history,  # 9 > min(8) and 9 ≤ median(10) → neutral
        last_alert_kind="low",
        last_alert_price=8.0,
    )
    assert decision.alert_kind is None
    assert decision.new_state is None
    assert decision.new_state_price is None


def test_neutral_clears_existing_high_state():
    """Was above median, price drops back to median: no DM, state clears
    so the next above-median crossing fires a fresh HIGH."""
    history = [8.0, 10.0, 12.0, 9.0, 11.0, 10.0, 12.0]  # min=8, median=10
    decision = _classify_price(
        10.0, history,  # equals median, > min → neutral (high check is strict-greater)
        last_alert_kind="high",
        last_alert_price=15.0,
    )
    assert decision.alert_kind is None
    assert decision.new_state is None
    assert decision.new_state_price is None


# ---------------------------------------------------------------------------
# Cross-zone transitions
# ---------------------------------------------------------------------------

def test_low_to_high_in_one_pass_fires_high():
    history = [10.0, 11.0, 10.0, 11.0, 10.0, 11.0, 10.0]  # min=10, median=10
    decision = _classify_price(
        20.0, history,
        last_alert_kind="low",
        last_alert_price=10.0,
    )
    assert decision.alert_kind == "high"
    assert decision.new_state == "high"


def test_high_to_low_in_one_pass_fires_low():
    history = [10.0, 11.0, 10.0, 11.0, 10.0, 11.0, 10.0]
    decision = _classify_price(
        5.0, history,
        last_alert_kind="high",
        last_alert_price=20.0,
    )
    assert decision.alert_kind == "low"
    assert decision.new_state == "low"
    assert decision.new_state_price == 5.0


# ---------------------------------------------------------------------------
# Context fields (so DM formatter has what it needs)
# ---------------------------------------------------------------------------

def test_decision_carries_all_time_low_and_median_when_data_sufficient():
    history = [10.0, 12.0, 11.0, 9.0, 13.0, 10.0, 11.0]
    decision = _classify_price(8.0, history, None, None)
    assert decision.all_time_low == 9.0
    # median of sorted [9,10,10,11,11,12,13] is 11.
    assert decision.median_price == 11.0
    assert decision.prev_alert_price is None


def test_decision_returns_dataclass_instance():
    """Sanity: callers rely on attribute access, not tuple indexing."""
    decision = _classify_price(None, [], None, None)
    assert isinstance(decision, AlertDecision)
