"""Persistence operations for wishlist items, prices, and exchange rates."""

from __future__ import annotations

import logging
import time

from db.connection import _connect

logger = logging.getLogger("database")

def add_scraped_item(user_id, url, title=None, price=None, stock=1, currency=None):
    """Insert a new tracked item.

    `stock` is tri-state: True/1 → in stock, False/0 → out of stock,
    None → unknown. Unknown is stored as NULL so future scrapes that read
    `None` don't get conflated with "definitely out of stock".
    """
    try:
        stock_int = None if stock is None else (1 if stock else 0)
        with _connect(commit=True) as c:
            c.execute(
                "INSERT OR IGNORE INTO scraped_items (user_id, url, title, last_price, last_stock_status, currency) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, url, title, price, stock_int, currency)
            )
            if c.rowcount > 0:
                item_id = c.lastrowid
                return item_id
        return None
    except Exception:
        logger.exception(f"Failed to add scrape item: {url}")
        return None

def delete_scraped_item(user_id, url):
    try:
        with _connect(commit=True) as c:
            # `PRAGMA foreign_keys = ON` (set in `_connect`) now makes the
            # `ON DELETE CASCADE` on `price_history.item_id` work, so the
            # explicit `DELETE FROM price_history` is no longer strictly
            # required. Kept as belt-and-braces in case a future caller
            # opens its own connection and forgets the pragma.
            c.execute("SELECT id FROM scraped_items WHERE user_id = ? AND url = ?", (user_id, url))
            row = c.fetchone()
            if row:
                item_id = row[0]
                c.execute("DELETE FROM price_history WHERE item_id = ?", (item_id,))
                c.execute("DELETE FROM scraped_items WHERE id = ?", (item_id,))
                return True
        return False
    except Exception:
        logger.exception(f"Failed to delete scrape item: {url}")
        return False

def get_all_scraped_items():
    """Return every tracked item across all users.

    Tuple shape: `(id, user_id, url, last_price, last_stock_status, title,
    currency, last_alert_kind, last_alert_price, target_price,
    target_currency, target_alerted, restock_only, last_checked_at,
    last_check_status)` — 15 fields. The alert/preference columns are used
    exclusively by the scrape loop; no page contents are persisted.
    """
    try:
        with _connect() as c:
            c.execute(
                "SELECT id, user_id, url, last_price, last_stock_status, title, "
                "currency, last_alert_kind, last_alert_price, target_price, "
                "target_currency, target_alerted, restock_only, last_checked_at, "
                "last_check_status FROM scraped_items"
            )
            return c.fetchall()
    except Exception:
        logger.exception("Failed to fetch all scraped items")
        return []


def get_user_scraped_items_for_refresh(user_id):
    """Return all of one user's items in the scrape-loop tuple shape.

    This is deliberately separate from the legacy display projection so a
    manual all-items refresh can reuse ``_manual_refresh_item`` without ever
    loading another user's wishlist.
    """
    try:
        with _connect() as c:
            c.execute(
                "SELECT id, user_id, url, last_price, last_stock_status, title, "
                "currency, last_alert_kind, last_alert_price, target_price, "
                "target_currency, target_alerted, restock_only, last_checked_at, "
                "last_check_status FROM scraped_items WHERE user_id = ?",
                (user_id,),
            )
            return c.fetchall()
    except Exception:
        logger.exception(f"Failed to fetch refreshable scraped items for user {user_id}")
        return []


def get_user_scraped_items(user_id):
    """Return the legacy five-field display projection for graph callers."""
    try:
        with _connect() as c:
            c.execute("SELECT url, last_price, last_stock_status, title, currency FROM scraped_items WHERE user_id = ?", (user_id,))
            return c.fetchall()
    except Exception:
        logger.exception(f"Failed to fetch scraped items for user {user_id}")
        return []


def get_user_scraped_items_with_settings(user_id):
    """Return display rows plus alert preferences and freshness metadata.

    Tuple shape: `(url, price, stock, title, currency, target_price,
    target_currency, restock_only, last_checked_at, last_check_status)`.
    Kept separate from `get_user_scraped_items` so existing graph consumers
    retain their intentionally small, stable tuple shape.
    """
    try:
        with _connect() as c:
            c.execute(
                "SELECT url, last_price, last_stock_status, title, currency, "
                "target_price, target_currency, restock_only, last_checked_at, "
                "last_check_status FROM scraped_items WHERE user_id = ?",
                (user_id,),
            )
            return c.fetchall()
    except Exception:
        logger.exception(f"Failed to fetch detailed scraped items for user {user_id}")
        return []


def get_scraped_item(user_id, url):
    """Return one owned item in the same shape as `get_all_scraped_items`."""
    try:
        with _connect() as c:
            c.execute(
                "SELECT id, user_id, url, last_price, last_stock_status, title, "
                "currency, last_alert_kind, last_alert_price, target_price, "
                "target_currency, target_alerted, restock_only, last_checked_at, "
                "last_check_status FROM scraped_items WHERE user_id = ? AND url = ?",
                (user_id, url),
            )
            return c.fetchone()
    except Exception:
        logger.exception(f"Failed to fetch scraped item for {url}")
        return None


def set_scraped_item_target(user_id, url, price, currency):
    """Set (or clear with `price=None`) an owned item's threshold alert.

    Changing a target always re-arms it. This avoids a previous target's alert
    state suppressing an alert for the newly configured threshold.
    """
    try:
        with _connect(commit=True) as c:
            c.execute(
                "UPDATE scraped_items SET target_price = ?, target_currency = ?, "
                "target_alerted = 0 WHERE user_id = ? AND url = ?",
                (price, currency.upper() if currency else None, user_id, url),
            )
            return c.rowcount > 0
    except Exception:
        logger.exception(f"Failed to set target price for {url}")
        return False


def set_scraped_item_restock_only(user_id, url, enabled):
    try:
        with _connect(commit=True) as c:
            c.execute(
                "UPDATE scraped_items SET restock_only = ? WHERE user_id = ? AND url = ?",
                (1 if enabled else 0, user_id, url),
            )
            return c.rowcount > 0
    except Exception:
        logger.exception(f"Failed to set restock-only mode for {url}")
        return False


def update_scraped_item_check_status(item_id, status):
    """Record the outcome of the most recent attempt without changing data."""
    try:
        with _connect(commit=True) as c:
            c.execute(
                "UPDATE scraped_items SET last_checked_at = ?, last_check_status = ? "
                "WHERE id = ?",
                (time.time(), status, item_id),
            )
    except Exception:
        logger.exception(f"Failed to update scrape check status for ID {item_id}")


def update_scraped_item_target_state(item_id, alerted):
    try:
        with _connect(commit=True) as c:
            c.execute(
                "UPDATE scraped_items SET target_alerted = ? WHERE id = ?",
                (1 if alerted else 0, item_id),
            )
    except Exception:
        logger.exception(f"Failed to update target alert state for ID {item_id}")

def update_scraped_item_status(item_id, price, in_stock, title=None, currency=None):
    """Update a tracked item's latest snapshot. Every field is COALESCEd, so
    passing `None` for any one of them preserves the previously-stored value
    rather than clobbering it.

    `in_stock` is tri-state (True / False / None=unknown) — None leaves
    `last_stock_status` untouched, which is what callers want when the
    scraper couldn't determine stock status on this pass.
    """
    try:
        stock_int = None if in_stock is None else (1 if in_stock else 0)
        with _connect(commit=True) as c:
            c.execute(
                "UPDATE scraped_items SET "
                "last_price = COALESCE(?, last_price), "
                "last_stock_status = COALESCE(?, last_stock_status), "
                "title = COALESCE(?, title), "
                "currency = COALESCE(?, currency) "
                "WHERE id = ?",
                (price, stock_int, title, currency, item_id)
            )
    except Exception:
        logger.exception(f"Failed to update scraped item status for ID {item_id}")

def update_item_alert_state(item_id, kind, price):
    """Persist the LOW/HIGH alert state for an item.

    `kind` is "low" / "high" / None — the zone the item's price is in
    after the latest scrape. `price` is the price at the moment of the
    last alert (used for the LOW re-alert threshold) or None when state
    is being cleared back to neutral.

    Called from `_process_scrape_item` after every scrape pass, so kept
    as a single fast UPDATE.
    """
    try:
        with _connect(commit=True) as c:
            c.execute(
                "UPDATE scraped_items SET last_alert_kind = ?, last_alert_price = ? WHERE id = ?",
                (kind, price, item_id),
            )
    except Exception:
        logger.exception(f"Failed to update alert state for item {item_id}")

def add_price_history(item_id, price):
    try:
        with _connect(commit=True) as c:
            c.execute(
                "INSERT INTO price_history (item_id, price, timestamp) VALUES (?, ?, ?)",
                (item_id, price, time.time())
            )
    except Exception:
        logger.exception(f"Failed to add price history for item {item_id}")

def clean_old_price_history(days=180):
    """Trim `price_history` to a rolling `days`-long window per item.

    Called once per scrape pass (every 12 h from `_scrape_loop`). Each call
    deletes rows whose timestamp is older than `now - days`, so the table
    size stabilises at roughly
    `(scrape passes per day) × days × (tracked items)` rows once the bot
    has been running longer than the retention window. At the default of
    180 days, 12 h cadence, and 100 tracked items that's ~36 k rows /
    ~2.5 MB — well within SQLite's practical limits.

    **This is a sliding window, not a hard cutoff.** An item tracked for
    two years always shows its most recent `days` of price changes; it
    does NOT get wiped on its 180-day anniversary. Each day, the oldest
    day's snapshots quietly drop off as the newest day's arrive — the
    user-visible behaviour is equivalent to a circular buffer of the
    most recent `days` of history, kept fresh on every scrape pass.

    History is only lost when:
      - the item is deleted via `/wishlist-item-delete` (FK cascade), or
      - the bot is offline longer than `days` (catch-up scrape's cleanup
        legitimately drops everything older than the new cutoff).

    Backed by `idx_price_history_timestamp` (see `init_db`) so the DELETE
    stays O(log n + k) even when the table holds many months of history.
    """
    try:
        cutoff = time.time() - (days * 86400)
        with _connect(commit=True) as c:
            c.execute("DELETE FROM price_history WHERE timestamp < ?", (cutoff,))
    except Exception:
        logger.exception("Failed to clean old price history")

# --- Exchange Rate functions ---

def set_exchange_rate(currency, rate_to_eur):
    """Persist `rate_to_eur` for `currency` (the API's native format:
    "how many units of `currency` are in 1 EUR")."""
    try:
        with _connect(commit=True) as c:
            c.execute(
                "INSERT OR REPLACE INTO exchange_rates (currency, rate_to_eur, last_updated) VALUES (?, ?, ?)",
                (currency.upper(), rate_to_eur, time.time())
            )
    except Exception:
        logger.exception(f"Failed to set exchange rate for {currency}")

def get_exchange_rate(currency):
    """Return how many units of `currency` make 1 EUR, or None if we
    don't have a stored rate for it."""
    try:
        with _connect() as c:
            c.execute("SELECT rate_to_eur FROM exchange_rates WHERE currency = ?", (currency.upper(),))
            row = c.fetchone()
            return row[0] if row else None
    except Exception:
        logger.exception(f"Failed to get exchange rate for {currency}")
        return None

def get_price_history(user_id, url):
    try:
        with _connect() as c:
            c.execute("""
                SELECT ph.price, ph.timestamp, si.title
                FROM price_history ph
                JOIN scraped_items si ON ph.item_id = si.id
                WHERE si.user_id = ? AND si.url = ?
                ORDER BY ph.timestamp ASC
            """, (user_id, url))
            return c.fetchall()
    except Exception:
        logger.exception(f"Failed to fetch price history for {url}")
        return []

