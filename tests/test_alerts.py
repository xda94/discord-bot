"""Tests for the buy-signal alert classifier in `features.scraping`.

`_classify_price` is the pure decision function the scrape loop calls
on every pass to decide whether to fire a LOW ("buy now") or HIGH
("maybe wait") DM. These tests exhaustively cover the state machine
so we won't accidentally re-introduce spammy or missed alerts on a
future refactor.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

import db

from features.scraping import (
    ALERT_LOW_REALERT_DROP_PCT,
    ALERT_MIN_DATA_POINTS,
    AlertDecision,
    ScrapeResult,
    ScrapingFeature,
    _classify_price,
)


def test_wishlist_refresh_refreshes_all_eligible_owned_items(tmp_db, monkeypatch):
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    feature = ScrapingFeature(client, tree)
    first_url = "https://shop.example/first"
    cooling_url = "https://shop.example/cooling"
    other_user_url = "https://shop.example/not-mine"
    db.add_scraped_item(123, first_url)
    db.add_scraped_item(123, cooling_url)
    db.add_scraped_item(456, other_user_url)

    feature._manual_refresh_at[(123, cooling_url)] = 950.0
    feature._manual_refresh_item = AsyncMock(return_value="Refreshed: First item")
    monkeypatch.setattr("features.scraping.time.monotonic", lambda: 1_000.0)
    monkeypatch.setattr("features.scraping.asyncio.sleep", AsyncMock())

    interaction = MagicMock()
    interaction.user.id = 123
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()

    refresh = tree.get_command("wishlist-refresh")
    asyncio.run(refresh.callback(interaction))

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    assert feature._manual_refresh_item.await_count == 1
    assert feature._manual_refresh_item.await_args.args[0][2] == first_url
    message = interaction.followup.send.await_args.args[0]
    assert "Refreshed 1 of 2 tracked item(s)." in message
    assert "Skipped 1 item(s) still on cooldown" in message
    assert "Refreshed: First item" in message


def test_wishlist_refresh_with_url_refreshes_only_that_owned_item(tmp_db):
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    feature = ScrapingFeature(client, tree)
    first_url = "https://shop.example/first"
    selected_url = "https://shop.example/selected"
    db.add_scraped_item(123, first_url)
    db.add_scraped_item(123, selected_url)

    feature._manual_refresh_item = AsyncMock(return_value="Refreshed: Selected item")
    interaction = MagicMock()
    interaction.user.id = 123
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()

    refresh = tree.get_command("wishlist-refresh")
    asyncio.run(refresh.callback(interaction, url=selected_url))

    assert feature._manual_refresh_item.await_count == 1
    assert feature._manual_refresh_item.await_args.args[0][2] == selected_url
    message = interaction.followup.send.await_args.args[0]
    assert "Refreshed the requested item." in message
    assert "Refreshed: Selected item" in message


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


def test_target_price_uses_configured_currency(monkeypatch):
    feature = object.__new__(ScrapingFeature)
    feature.converter = MagicMock()
    feature.converter.to_currency.return_value = 399.5

    reached, converted = feature._target_price_reached(
        80.0, "EUR", 400.0, "RON"
    )

    assert reached is True
    assert converted == 399.5
    feature.converter.to_currency.assert_called_once_with(80.0, "EUR", "RON")


def test_target_price_preserves_state_when_conversion_is_unavailable():
    feature = object.__new__(ScrapingFeature)
    feature.converter = MagicMock()
    feature.converter.to_currency.return_value = None

    reached, converted = feature._target_price_reached(
        80.0, "EUR", 400.0, "RON"
    )

    assert reached is None
    assert converted is None


def test_price_change_dm_includes_llm_reaction(monkeypatch):
    feature = object.__new__(ScrapingFeature)
    feature.scraper = MagicMock()
    feature.scraper.fetch.return_value = ScrapeResult(
        price=80.0,
        in_stock=True,
        title="Coffee machine",
        currency="RON",
    )
    feature.converter = MagicMock()
    feature.converter.format_with_conversions.side_effect = [
        "100.00 RON",
        "80.00 RON",
    ]
    user = MagicMock()
    user.send = AsyncMock()
    feature.client = MagicMock()
    feature.client.fetch_user = AsyncMock(return_value=user)
    generate_message = MagicMock(return_value="The price finally chose kindness.")

    monkeypatch.setattr("features.scraping.db.get_price_history", lambda *args: [])
    monkeypatch.setattr("features.scraping.db.add_price_history", MagicMock())
    monkeypatch.setattr("features.scraping.db.update_scraped_item_status", MagicMock())
    monkeypatch.setattr("features.scraping.db.update_item_alert_state", MagicMock())
    monkeypatch.setattr(
        "features.scraping.generate_price_change_message", generate_message
    )

    item = (
        1,
        2,
        "https://example.ro/coffee",
        100.0,
        1,
        "Old title",
        "RON",
        None,
        None,
    )
    asyncio.run(feature._process_scrape_item(item))

    generate_message.assert_called_once_with(
        "Coffee machine",
        100.0,
        80.0,
        "100.00 RON",
        "80.00 RON",
    )
    sent_message = user.send.await_args.args[0]
    assert "Price changed: `100.00 RON` -> **80.00 RON**" in sent_message
    assert "The price finally chose kindness." in sent_message


def test_price_change_dm_survives_missing_llm_reaction(monkeypatch):
    feature = object.__new__(ScrapingFeature)
    feature.scraper = MagicMock()
    feature.scraper.fetch.return_value = ScrapeResult(
        price=120.0,
        in_stock=True,
        title="Coffee machine",
        currency="RON",
    )
    feature.converter = MagicMock()
    feature.converter.format_with_conversions.side_effect = [
        "100.00 RON",
        "120.00 RON",
    ]
    user = MagicMock()
    user.send = AsyncMock()
    feature.client = MagicMock()
    feature.client.fetch_user = AsyncMock(return_value=user)

    monkeypatch.setattr("features.scraping.db.get_price_history", lambda *args: [])
    monkeypatch.setattr("features.scraping.db.add_price_history", MagicMock())
    monkeypatch.setattr("features.scraping.db.update_scraped_item_status", MagicMock())
    monkeypatch.setattr("features.scraping.db.update_item_alert_state", MagicMock())
    monkeypatch.setattr(
        "features.scraping.generate_price_change_message", lambda *args: None
    )

    item = (
        1,
        2,
        "https://example.ro/coffee",
        100.0,
        1,
        "Old title",
        "RON",
        None,
        None,
    )
    asyncio.run(feature._process_scrape_item(item))

    sent_message = user.send.await_args.args[0]
    assert "Price changed: `100.00 RON` -> **120.00 RON**" in sent_message
    assert "🤖" not in sent_message
