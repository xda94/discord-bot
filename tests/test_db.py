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
    db.set_exchange_rate("EUR", 7.45)
    assert db.get_exchange_rate("EUR") == 7.45
    # Lookup is case-insensitive (storage normalises to upper).
    assert db.get_exchange_rate("eur") == 7.45
    assert db.get_exchange_rate("XYZ") is None


# ---------------------------------------------------------------------------
# get_all_jokes regression: returns [] (not None) on error
# ---------------------------------------------------------------------------

def test_get_all_jokes_empty_when_no_data(tmp_db):
    """Smoke test that we get an empty list, not None — the API's `/jokes`
    route iterates the result and would crash on None."""
    result = db.get_all_jokes()
    assert result == []
