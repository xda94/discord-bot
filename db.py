import sqlite3
import random
import time
import logging
from contextlib import contextmanager

logger = logging.getLogger("database")

DB_FILE = "responses.db"


@contextmanager
def _connect(commit=False):
    conn = sqlite3.connect(DB_FILE)
    # SQLite ships with foreign-key enforcement OFF for backwards compat. The
    # pragma is per-connection, so it has to be set every time we open one,
    # not just once in `init_db`. Without this, `ON DELETE CASCADE` on
    # `price_history.item_id` is silently ignored.
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn.cursor()
        if commit:
            conn.commit()
    finally:
        conn.close()


def init_db():
    try:
        with _connect(commit=True) as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS responses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword TEXT NOT NULL,
                    response TEXT NOT NULL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    remind_at REAL NOT NULL,
                    message TEXT NOT NULL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS keyword_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    guild_id INTEGER NOT NULL,
                    used_at REAL NOT NULL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS jokes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    sent INTEGER NOT NULL DEFAULT 0
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS scraped_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    title TEXT,
                    currency TEXT,
                    last_price REAL,
                    last_stock_status INTEGER DEFAULT 1,
                    UNIQUE(user_id, url)
                )
            """)

            c.execute("PRAGMA table_info(scraped_items)")
            columns = [info[1] for info in c.fetchall()]
            if "title" not in columns:
                c.execute("ALTER TABLE scraped_items ADD COLUMN title TEXT")
            if "currency" not in columns:
                c.execute("ALTER TABLE scraped_items ADD COLUMN currency TEXT")

            c.execute("""
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    price REAL,
                    timestamp REAL NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES scraped_items(id) ON DELETE CASCADE
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS exchange_rates (
                    currency TEXT PRIMARY KEY,
                    rate_to_dkk REAL NOT NULL,
                    last_updated REAL NOT NULL
                )
            """)
            # Per-guild last-activity timestamp used by `InactivityFeature` to
            # decide when to nudge a quiet channel. Persisted so the 24h
            # threshold survives bot restarts.
            c.execute("""
                CREATE TABLE IF NOT EXISTS guild_activity (
                    guild_id INTEGER PRIMARY KEY,
                    last_time REAL NOT NULL,
                    channel_id INTEGER NOT NULL
                )
            """)
        logger.info("Database initialized.")
    except Exception:
        logger.exception("Critical error initializing database")


# --- Response functions ---
#
# `get_all_responses()` runs on EVERY message the bot sees, doing a full
# `SELECT * FROM responses`. To avoid hammering SQLite on a chatty server we
# memoise the result for a short TTL and invalidate explicitly whenever
# *this process* mutates the table (add/remove). Cross-process mutations
# (e.g. via the Flask API in `api.py`) become visible at most TTL seconds
# later — well below user-perceptible for "I just added a keyword".

_RESPONSES_CACHE_TTL = 30.0  # seconds
_responses_cache: dict | None = None
_responses_cache_at: float = 0.0


def _invalidate_responses_cache():
    global _responses_cache
    _responses_cache = None


def add_response(keyword, response):
    try:
        with _connect(commit=True) as c:
            c.execute("INSERT INTO responses (keyword, response) VALUES (?, ?)",
                      (keyword.lower(), response))
        logger.info(f"Inserted new response for keyword: '{keyword}'")
        _invalidate_responses_cache()
    except Exception:
        logger.exception(f"Failed to add response for '{keyword}'")


def remove_response(keyword, response=None):
    try:
        with _connect(commit=True) as c:
            if response is None:
                c.execute("DELETE FROM responses WHERE keyword = ?", (keyword.lower(),))
            else:
                c.execute("DELETE FROM responses WHERE keyword = ? AND response = ?",
                          (keyword.lower(), response))
            deleted = c.rowcount
        logger.info(f"Deleted {deleted} response(s) for keyword: '{keyword}'")
        if deleted:
            _invalidate_responses_cache()
        return deleted > 0
    except Exception:
        logger.exception(f"Failed to remove response for '{keyword}'")
        return False


def get_all_responses():
    global _responses_cache, _responses_cache_at
    now = time.time()
    if _responses_cache is not None and (now - _responses_cache_at) < _RESPONSES_CACHE_TTL:
        return _responses_cache
    try:
        with _connect() as c:
            c.execute("SELECT keyword, response FROM responses")
            data = c.fetchall()
        result: dict = {}
        for keyword, response in data:
            result.setdefault(keyword.lower(), []).append(response)
        _responses_cache = result
        _responses_cache_at = now
        return result
    except Exception:
        logger.exception("Failed to fetch all responses")
        # Don't poison the cache with an empty dict — let the next call retry.
        return {}


def get_random_response(keyword):
    try:
        with _connect() as c:
            c.execute("SELECT response FROM responses WHERE keyword = ?", (keyword.lower(),))
            rows = c.fetchall()
        if not rows:
            return None
        return random.choice(rows)[0]
    except Exception:
        logger.exception(f"Error retrieving random response for '{keyword}'")
        return None


# --- Reminder functions ---

def add_reminder(user_id, channel_id, remind_at, message):
    try:
        with _connect(commit=True) as c:
            c.execute("INSERT INTO reminders (user_id, channel_id, remind_at, message) VALUES (?, ?, ?, ?)",
                      (user_id, channel_id, remind_at, message))
        logger.info(f"Scheduled reminder for user {user_id} at timestamp {remind_at}")
    except Exception:
        logger.exception("Failed to add reminder")


def get_due_reminders():
    try:
        now = time.time()
        with _connect() as c:
            c.execute("SELECT id, user_id, channel_id, message FROM reminders WHERE remind_at <= ?", (now,))
            return c.fetchall()
    except Exception:
        logger.exception("Failed to fetch due reminders")
        return []


def delete_reminder(reminder_id):
    try:
        with _connect(commit=True) as c:
            c.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        logger.debug(f"Deleted reminder ID: {reminder_id}")
    except Exception:
        logger.exception(f"Failed to delete reminder ID {reminder_id}")


def get_all_reminders():
    try:
        with _connect() as c:
            c.execute("SELECT id, user_id, channel_id, message, remind_at FROM reminders")
            return c.fetchall()
    except Exception:
        logger.exception("Failed to fetch all reminders")
        return []


# --- Keyword usage functions ---

def log_keyword_usage(keyword, user_id, guild_id):
    try:
        with _connect(commit=True) as c:
            c.execute("INSERT INTO keyword_usage (keyword, user_id, guild_id, used_at) VALUES (?, ?, ?, ?)",
                      (keyword.lower(), user_id, guild_id, time.time()))
        logger.debug(f"Logged usage of keyword '{keyword}' by user {user_id}")
    except Exception:
        logger.exception(f"Failed to log keyword usage for '{keyword}'")


def get_top_keywords(guild_id, limit=10):
    try:
        with _connect() as c:
            c.execute(
                "SELECT keyword, COUNT(*) as cnt FROM keyword_usage "
                "WHERE guild_id = ? GROUP BY keyword ORDER BY cnt DESC LIMIT ?",
                (guild_id, limit))
            rows = c.fetchall()
        logger.info(f"Fetched top {limit} keywords for guild {guild_id}")
        return rows
    except Exception:
        logger.exception("Failed to fetch top keywords")
        return []


def get_top_keywords_by_user(guild_id, user_id, limit=10):
    try:
        with _connect() as c:
            c.execute(
                "SELECT keyword, COUNT(*) as cnt FROM keyword_usage "
                "WHERE guild_id = ? AND user_id = ? GROUP BY keyword ORDER BY cnt DESC LIMIT ?",
                (guild_id, user_id, limit))
            rows = c.fetchall()
        logger.info(f"Fetched top {limit} keywords for user {user_id} in guild {guild_id}")
        return rows
    except Exception:
        logger.exception(f"Failed to fetch top keywords for user {user_id}")
        return []


# --- Joke functions ---

def add_joke(text):
    try:
        with _connect(commit=True) as c:
            c.execute("INSERT INTO jokes (text, sent) VALUES (?, 0)", (text,))
        logger.info(f"Added new joke: '{text[:50]}...'")
    except Exception:
        logger.exception("Failed to add joke")


def get_unsent_joke():
    try:
        with _connect() as c:
            c.execute("SELECT id, text FROM jokes WHERE sent = 0")
            rows = c.fetchall()
        if not rows:
            return None
        return random.choice(rows)
    except Exception:
        logger.exception("Failed to get unsent joke")
        return None


def mark_joke_sent(joke_id):
    try:
        with _connect(commit=True) as c:
            c.execute("UPDATE jokes SET sent = 1 WHERE id = ?", (joke_id,))
        logger.debug(f"Marked joke {joke_id} as sent")
    except Exception:
        logger.exception(f"Failed to mark joke {joke_id} as sent")


def reset_jokes():
    try:
        with _connect(commit=True) as c:
            c.execute("UPDATE jokes SET sent = 0")
        logger.info("Reset all jokes to unsent")
    except Exception:
        logger.exception("Failed to reset jokes")


def get_all_jokes():
    try:
        with _connect() as c:
            c.execute("SELECT id, text, sent FROM jokes")
            return c.fetchall()
    except Exception:
        logger.exception("Failed to fetch all jokes")
        # Return an empty list instead of None so callers (e.g. the Flask
        # API's `/jokes` route) can iterate the result unconditionally.
        return []


# --- Settings functions ---

def get_setting(key):
    try:
        with _connect() as c:
            c.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = c.fetchone()
        return row[0] if row else None
    except Exception:
        logger.exception(f"Failed to get setting '{key}'")
        return None


def set_setting(key, value):
    try:
        with _connect(commit=True) as c:
            c.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
        logger.info(f"Setting '{key}' set to '{value}'")
    except Exception:
        logger.exception(f"Failed to set setting '{key}'")


def get_joke_settings():
    channel_id = get_setting("joke_channel_id")
    send_time = get_setting("joke_send_time")
    return {
        "channel_id": int(channel_id) if channel_id else None,
        "send_time": send_time or "12:00",
    }


def get_joke_by_id(joke_id):
    try:
        with _connect() as c:
            c.execute("SELECT id, text, sent FROM jokes WHERE id = ?", (joke_id,))
            return c.fetchone()
    except Exception:
        logger.exception(f"Failed to fetch joke {joke_id}")
        return None


def update_joke(joke_id, text):
    try:
        with _connect(commit=True) as c:
            c.execute("UPDATE jokes SET text = ? WHERE id = ?", (text, joke_id))
            updated = c.rowcount
        logger.info(f"Updated joke {joke_id}")
        return updated > 0
    except Exception:
        logger.exception(f"Failed to update joke {joke_id}")
        return False


def delete_joke(joke_id):
    try:
        with _connect(commit=True) as c:
            c.execute("DELETE FROM jokes WHERE id = ?", (joke_id,))
            deleted = c.rowcount
        logger.info(f"Deleted joke {joke_id}")
        return deleted > 0
    except Exception:
        logger.exception(f"Failed to delete joke {joke_id}")
        return False

# --- Scrape functions ---

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
    try:
        with _connect() as c:
            c.execute("SELECT id, user_id, url, last_price, last_stock_status, title, currency FROM scraped_items")
            return c.fetchall()
    except Exception:
        logger.exception("Failed to fetch all scraped items")
        return []

def get_user_scraped_items(user_id):
    try:
        with _connect() as c:
            c.execute("SELECT url, last_price, last_stock_status, title, currency FROM scraped_items WHERE user_id = ?", (user_id,))
            return c.fetchall()
    except Exception:
        logger.exception(f"Failed to fetch scraped items for user {user_id}")
        return []

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

def add_price_history(item_id, price):
    try:
        with _connect(commit=True) as c:
            c.execute(
                "INSERT INTO price_history (item_id, price, timestamp) VALUES (?, ?, ?)",
                (item_id, price, time.time())
            )
    except Exception:
        logger.exception(f"Failed to add price history for item {item_id}")

def clean_old_price_history(days=5):
    try:
        cutoff = time.time() - (days * 86400)
        with _connect(commit=True) as c:
            c.execute("DELETE FROM price_history WHERE timestamp < ?", (cutoff,))
    except Exception:
        logger.exception("Failed to clean old price history")

# --- Exchange Rate functions ---

def set_exchange_rate(currency, rate_to_dkk):
    try:
        with _connect(commit=True) as c:
            c.execute(
                "INSERT OR REPLACE INTO exchange_rates (currency, rate_to_dkk, last_updated) VALUES (?, ?, ?)",
                (currency.upper(), rate_to_dkk, time.time())
            )
    except Exception:
        logger.exception(f"Failed to set exchange rate for {currency}")

def get_exchange_rate(currency):
    try:
        with _connect() as c:
            c.execute("SELECT rate_to_dkk FROM exchange_rates WHERE currency = ?", (currency.upper(),))
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


# --- Guild activity functions (used by InactivityFeature) ---

def set_guild_activity(guild_id, last_time, channel_id):
    """UPSERT the most recent activity for a guild. Called on every message,
    so kept as a single fast statement."""
    try:
        with _connect(commit=True) as c:
            c.execute(
                "INSERT INTO guild_activity (guild_id, last_time, channel_id) VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET "
                "last_time = excluded.last_time, channel_id = excluded.channel_id",
                (guild_id, last_time, channel_id),
            )
    except Exception:
        logger.exception(f"Failed to set guild_activity for guild {guild_id}")


def get_all_guild_activity():
    """Return `[(guild_id, last_time, channel_id), ...]` for every known
    guild. `InactivityFeature` loads this once on startup to repopulate its
    in-memory cache so the 24h nudge threshold survives restarts."""
    try:
        with _connect() as c:
            c.execute("SELECT guild_id, last_time, channel_id FROM guild_activity")
            return c.fetchall()
    except Exception:
        logger.exception("Failed to fetch guild_activity")
        return []
