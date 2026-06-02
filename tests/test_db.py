"""Tests for `db.py`.

Covers the surfaces most likely to silently regress on future refactors:

  - Responses CRUD round-trips.
  - The new TTL cache in `get_all_responses` (in-process invalidation +
    cross-call object identity within the window + re-fetch after expiry).
  - Tri-state stock storage on `add_scraped_item` and the new `COALESCE`
    semantics on `update_scraped_item_status`.
  - Foreign-key cascade between `scraped_items` and `price_history` now
    that `PRAGMA foreign_keys = ON` is set inside `_connect`.
  - Guild activity UPSERT round-trip.
  - Exchange-rate round-trip with case-insensitive lookup.
"""

import db


# ---------------------------------------------------------------------------
# Responses CRUD
# ---------------------------------------------------------------------------

def test_responses_add_get_remove(tmp_db):
    db.add_response("hello", "world")
    db.add_response("hello", "earth")

    assert db.get_all_responses() == {"hello": ["world", "earth"]}

    assert db.remove_response("hello", "world")
    assert db.get_all_responses() == {"hello": ["earth"]}

    assert db.remove_response("hello")
    assert db.get_all_responses() == {}


def test_remove_response_returns_false_when_missing(tmp_db):
    assert db.remove_response("never_added") is False


# ---------------------------------------------------------------------------
# Response TTL cache
# ---------------------------------------------------------------------------

def test_cache_invalidated_immediately_on_add(tmp_db):
    """An add in this process must be visible on the very next read,
    regardless of TTL — `add_response` calls `_invalidate_responses_cache`."""
    db.get_all_responses()  # warm the cache (empty)
    db.add_response("kw", "value")
    assert db.get_all_responses() == {"kw": ["value"]}


def test_cache_invalidated_immediately_on_remove(tmp_db):
    db.add_response("kw", "value")
    db.get_all_responses()  # warm the cache
    db.remove_response("kw")
    assert db.get_all_responses() == {}


def test_cache_hits_return_same_object_within_ttl(tmp_db):
    """Two consecutive reads inside the TTL window must hit the cache, not
    re-query SQLite. Use `is` to confirm we got the cached dict, not just
    an equal-valued copy."""
    db.add_response("kw", "value")
    first = db.get_all_responses()
    second = db.get_all_responses()
    assert first is second


def test_cache_miss_after_ttl_expires(tmp_db, monkeypatch):
    """When the TTL elapses we must re-read SQLite — same content but a
    fresh dict object."""
    db.add_response("kw", "value")
    first = db.get_all_responses()
    monkeypatch.setattr(db, "_RESPONSES_CACHE_TTL", 0.0)
    second = db.get_all_responses()
    assert first == second
    assert first is not second


# ---------------------------------------------------------------------------
# Scraped-item tri-state stock
# ---------------------------------------------------------------------------

def test_add_scraped_item_stock_true_stored_as_one(tmp_db):
    item_id = db.add_scraped_item(123, "https://example.com/a", stock=True)
    assert item_id
    items = db.get_user_scraped_items(123)
    assert len(items) == 1
    # Tuple shape: (url, last_price, last_stock_status, title, currency)
    assert items[0][2] == 1


def test_add_scraped_item_stock_false_stored_as_zero(tmp_db):
    db.add_scraped_item(123, "https://example.com/a", stock=False)
    items = db.get_user_scraped_items(123)
    assert items[0][2] == 0


def test_add_scraped_item_stock_none_stored_as_null(tmp_db):
    """The new tri-state — `None` means "couldn't determine" and must round-
    trip as SQL NULL, not get silently coerced to 0 (the old behaviour)."""
    db.add_scraped_item(123, "https://example.com/a", stock=None)
    items = db.get_user_scraped_items(123)
    assert items[0][2] is None


def test_add_scraped_item_rejects_duplicate_url(tmp_db):
    first = db.add_scraped_item(123, "https://example.com/a")
    second = db.add_scraped_item(123, "https://example.com/a")
    assert first
    assert second is None


# ---------------------------------------------------------------------------
# update_scraped_item_status: COALESCE semantics
# ---------------------------------------------------------------------------

def test_update_preserves_fields_passed_as_none(tmp_db):
    """All five user-supplied fields must be COALESCE'd — passing `None` for
    any one of them must preserve the previously-stored value rather than
    overwrite with NULL."""
    item_id = db.add_scraped_item(
        123, "https://example.com/x",
        title="Original", price=10.0, stock=True, currency="EUR",
    )
    # Partial update: only `price` carries new data.
    db.update_scraped_item_status(
        item_id, price=15.0, in_stock=None, title=None, currency=None,
    )
    url, price, stock, title, currency = db.get_user_scraped_items(123)[0]
    assert price == 15.0
    assert stock == 1
    assert title == "Original"
    assert currency == "EUR"


def test_update_overwrites_when_value_supplied(tmp_db):
    item_id = db.add_scraped_item(
        123, "https://example.com/x",
        title="Old", price=10.0, stock=True, currency="EUR",
    )
    db.update_scraped_item_status(
        item_id, price=20.0, in_stock=False, title="New", currency="USD",
    )
    url, price, stock, title, currency = db.get_user_scraped_items(123)[0]
    assert price == 20.0
    assert stock == 0
    assert title == "New"
    assert currency == "USD"


def test_update_stock_none_does_not_flip_status(tmp_db):
    """Regression for the in-stock loop bug: an unknown stock read (None)
    must NOT silently flip a known in-stock item to OOS."""
    item_id = db.add_scraped_item(123, "https://example.com/x", stock=True)
    db.update_scraped_item_status(item_id, price=10.0, in_stock=None)
    assert db.get_user_scraped_items(123)[0][2] == 1  # still in stock


# ---------------------------------------------------------------------------
# Foreign-key cascade on delete
# ---------------------------------------------------------------------------

def test_delete_scraped_item_removes_price_history(tmp_db):
    """With `PRAGMA foreign_keys = ON` (set in `_connect`) the cascade on
    `price_history.item_id` actually fires. The explicit cascade in
    `delete_scraped_item` makes this belt-and-braces."""
    item_id = db.add_scraped_item(123, "https://example.com/x")
    db.add_price_history(item_id, 10.0)
    db.add_price_history(item_id, 11.0)
    assert db.get_price_history(123, "https://example.com/x")

    assert db.delete_scraped_item(123, "https://example.com/x")
    assert db.get_price_history(123, "https://example.com/x") == []
    assert db.get_user_scraped_items(123) == []


# ---------------------------------------------------------------------------
# Guild activity (used by InactivityFeature)
# ---------------------------------------------------------------------------

def test_guild_activity_upsert_per_guild(tmp_db):
    db.set_guild_activity(guild_id=1, last_time=100.0, channel_id=10)
    db.set_guild_activity(guild_id=1, last_time=200.0, channel_id=20)
    db.set_guild_activity(guild_id=2, last_time=300.0, channel_id=30)

    rows = sorted(db.get_all_guild_activity())
    assert rows == [(1, 200.0, 20), (2, 300.0, 30)]


def test_guild_activity_returns_empty_when_unset(tmp_db):
    assert db.get_all_guild_activity() == []


# ---------------------------------------------------------------------------
# Exchange rates
# ---------------------------------------------------------------------------

def test_exchange_rate_round_trip(tmp_db):
    # Stored rate is "units of <currency> per 1 EUR" — for DKK that's
    # roughly 7.45. The round-trip just confirms persistence and the
    # case-normalisation behaviour.
    db.set_exchange_rate("DKK", 7.45)
    assert db.get_exchange_rate("DKK") == 7.45
    # Lookup is case-insensitive (storage normalises to upper).
    assert db.get_exchange_rate("dkk") == 7.45
    assert db.get_exchange_rate("XYZ") is None


# ---------------------------------------------------------------------------
# get_all_jokes regression: returns [] (not None) on error
# ---------------------------------------------------------------------------

def test_get_all_jokes_empty_when_no_data(tmp_db):
    """Smoke test that we get an empty list, not None — the API's `/jokes`
    route iterates the result and would crash on None."""
    result = db.get_all_jokes()
    assert result == []


# ---------------------------------------------------------------------------
# Per-guild joke schedule (guild_joke_config)
# ---------------------------------------------------------------------------

def test_guild_joke_config_round_trip(tmp_db):
    """Activate one guild, read it back, verify field shape."""
    db.set_guild_joke_config(guild_id=111, channel_id=222, send_time="09:00")
    cfg = db.get_guild_joke_config(111)
    assert cfg == {
        "guild_id": 111,
        "channel_id": 222,
        "send_time": "09:00",
        "last_sent_date": None,
    }


def test_guild_joke_config_upsert_overwrites_channel_and_time(tmp_db):
    """A second /joke_activation in the same guild should replace the
    schedule, not append a new row."""
    db.set_guild_joke_config(111, 222, "09:00")
    db.set_guild_joke_config(111, 333, "18:30")
    cfg = db.get_guild_joke_config(111)
    assert cfg["channel_id"] == 333
    assert cfg["send_time"] == "18:30"


def test_guild_joke_config_upsert_preserves_last_sent_date(tmp_db):
    """Re-activating on the same day must not clear `last_sent_date` —
    otherwise a guild that re-runs /joke_activation after the day's
    joke fired would get a duplicate the next time the loop ticks."""
    db.set_guild_joke_config(111, 222, "09:00")
    db.set_guild_joke_last_sent(111, "2026-06-02")
    # Re-activate with a different time but same day.
    db.set_guild_joke_config(111, 222, "11:00")
    cfg = db.get_guild_joke_config(111)
    assert cfg["last_sent_date"] == "2026-06-02"
    assert cfg["send_time"] == "11:00"


def test_get_guild_joke_config_returns_none_when_missing(tmp_db):
    assert db.get_guild_joke_config(99999) is None


def test_get_all_guild_joke_configs_returns_each_guild_once(tmp_db):
    db.set_guild_joke_config(111, 222, "09:00")
    db.set_guild_joke_config(333, 444, "14:00")
    db.set_guild_joke_config(555, 666, "20:00")
    configs = db.get_all_guild_joke_configs()
    by_guild = {c["guild_id"]: c for c in configs}
    assert set(by_guild) == {111, 333, 555}
    assert by_guild[333]["channel_id"] == 444
    assert by_guild[333]["send_time"] == "14:00"


def test_get_all_guild_joke_configs_empty_returns_list(tmp_db):
    """Loop in JokesFeature._check iterates this unconditionally; must
    be `[]`, not None, even when no guilds have activated."""
    assert db.get_all_guild_joke_configs() == []


def test_clear_guild_joke_config_returns_true_when_present(tmp_db):
    db.set_guild_joke_config(111, 222, "09:00")
    assert db.clear_guild_joke_config(111) is True
    assert db.get_guild_joke_config(111) is None


def test_clear_guild_joke_config_returns_false_when_missing(tmp_db):
    """Used by /joke_deactivation to detect 'no-op' and message the
    user differently."""
    assert db.clear_guild_joke_config(99999) is False


def test_clear_guild_joke_config_only_affects_target_guild(tmp_db):
    db.set_guild_joke_config(111, 222, "09:00")
    db.set_guild_joke_config(333, 444, "14:00")
    db.clear_guild_joke_config(111)
    assert db.get_guild_joke_config(111) is None
    assert db.get_guild_joke_config(333) is not None


def test_clear_guild_joke_config_preserves_sent_history(tmp_db):
    """Deactivation should NOT wipe sent history — if the user
    re-activates later, they shouldn't get repeats of jokes they
    already saw. Use reset_guild_joke_sent for a clean slate."""
    db.add_joke("joke 1")
    db.set_guild_joke_config(111, 222, "09:00")
    db.mark_guild_joke_sent(111, 1)
    db.clear_guild_joke_config(111)
    # Sent row still there.
    with db._connect() as c:
        c.execute("SELECT COUNT(*) FROM guild_joke_sent WHERE guild_id = ?", (111,))
        assert c.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Per-guild sent tracking (guild_joke_sent)
# ---------------------------------------------------------------------------

def test_get_unsent_joke_for_guild_returns_unsent(tmp_db):
    db.add_joke("a")
    db.add_joke("b")
    result = db.get_unsent_joke_for_guild(111)
    assert result is not None
    assert result[1] in ("a", "b")


def test_get_unsent_joke_for_guild_skips_already_sent_for_that_guild(tmp_db):
    db.add_joke("a")
    db.add_joke("b")
    db.mark_guild_joke_sent(111, 1)  # mark "a" as sent in guild 111
    # Repeatedly pick: must always return "b" (id=2) for guild 111.
    for _ in range(10):
        joke_id, text = db.get_unsent_joke_for_guild(111)
        assert joke_id == 2
        assert text == "b"


def test_get_unsent_joke_for_guild_returns_none_when_exhausted(tmp_db):
    """Caller (JokesFeature._send_joke_for_guild) relies on this None
    signal to trigger reset_guild_joke_sent + retry."""
    db.add_joke("a")
    db.mark_guild_joke_sent(111, 1)
    assert db.get_unsent_joke_for_guild(111) is None


def test_get_unsent_joke_for_guild_returns_none_when_jokes_table_empty(tmp_db):
    """No jokes at all — returns None regardless of guild."""
    assert db.get_unsent_joke_for_guild(111) is None


def test_sent_tracking_is_per_guild(tmp_db):
    """Same joke can be sent in different guilds independently — the
    whole reason guild_joke_sent exists instead of jokes.sent."""
    db.add_joke("only joke")
    db.mark_guild_joke_sent(111, 1)
    # Guild 222 hasn't sent anything — joke is still available there.
    result = db.get_unsent_joke_for_guild(222)
    assert result is not None
    assert result[0] == 1


def test_mark_guild_joke_sent_is_idempotent(tmp_db):
    """UPSERT on (guild_id, joke_id) — second mark just refreshes
    sent_at, doesn't raise UNIQUE constraint."""
    db.add_joke("a")
    db.mark_guild_joke_sent(111, 1)
    db.mark_guild_joke_sent(111, 1)  # must not raise
    with db._connect() as c:
        c.execute(
            "SELECT COUNT(*) FROM guild_joke_sent WHERE guild_id = ? AND joke_id = ?",
            (111, 1),
        )
        assert c.fetchone()[0] == 1


def test_reset_guild_joke_sent_only_clears_target_guild(tmp_db):
    db.add_joke("a")
    db.mark_guild_joke_sent(111, 1)
    db.mark_guild_joke_sent(222, 1)
    db.reset_guild_joke_sent(111)
    # Guild 111's pool is fresh; guild 222 still has the joke as sent.
    assert db.get_unsent_joke_for_guild(111) is not None
    assert db.get_unsent_joke_for_guild(222) is None


def test_reset_all_guild_joke_sent_clears_every_guild(tmp_db):
    """Used by `POST /jokes/reset` — must wipe sent history for every
    guild simultaneously so all pools recycle at once."""
    db.add_joke("a")
    db.mark_guild_joke_sent(111, 1)
    db.mark_guild_joke_sent(222, 1)
    db.reset_all_guild_joke_sent()
    assert db.get_unsent_joke_for_guild(111) is not None
    assert db.get_unsent_joke_for_guild(222) is not None


def test_deleting_a_joke_cascades_into_sent_history(tmp_db):
    """ON DELETE CASCADE on guild_joke_sent.joke_id keeps the table
    clean when /jokes/<id> DELETE is called. Without the cascade we'd
    accumulate orphan sent-rows referencing dead joke IDs."""
    db.add_joke("doomed")
    db.add_joke("survivor")
    db.mark_guild_joke_sent(111, 1)
    db.mark_guild_joke_sent(111, 2)
    db.delete_joke(1)
    with db._connect() as c:
        c.execute("SELECT joke_id FROM guild_joke_sent WHERE guild_id = ?", (111,))
        remaining = [row[0] for row in c.fetchall()]
    assert remaining == [2]
