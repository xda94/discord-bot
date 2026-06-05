import os
import sqlite3
import random
import time
import logging
from contextlib import contextmanager

logger = logging.getLogger("database")

# Configurable so the DB can live outside the code checkout — typical
# layouts on the host:
#   - In-repo (default):     ./responses.db                     (gitignored)
#   - Sibling data dir:      DB_FILE=../discord-bot-data/responses.db
#   - System-wide:           DB_FILE=/var/lib/discord-bot/responses.db
# Tests override this directly via `monkeypatch.setattr(db, "DB_FILE", ...)`
# in the `tmp_db` fixture, so the env-var indirection is invisible to them.
DB_FILE = os.getenv("DB_FILE", "responses.db")

# `sqlite3.connect()` creates the file itself, but raises if the parent
# directory is missing. This matters the first time the bot starts after
# DB_FILE has been pointed at a fresh location (e.g. a sibling data dir
# that doesn't exist yet). No-op when `DB_FILE` has no directory part.
_db_dir = os.path.dirname(DB_FILE)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)


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
                    guild_id INTEGER NOT NULL,
                    keyword TEXT NOT NULL,
                    response TEXT NOT NULL
                )
            """)
            # Per-guild keywords: each server has its own keyword → response
            # map. Legacy DBs created before this column existed get it via
            # ALTER below; rows left with NULL guild_id never match.
            c.execute("PRAGMA table_info(responses)")
            response_columns = {row[1] for row in c.fetchall()}
            if "guild_id" not in response_columns:
                c.execute("ALTER TABLE responses ADD COLUMN guild_id INTEGER")
                c.execute("SELECT COUNT(*) FROM responses WHERE guild_id IS NULL")
                legacy_count = c.fetchone()[0]
                if legacy_count:
                    logger.warning(
                        f"{legacy_count} legacy keyword(s) have no guild_id and "
                        f"will not match until re-added per server with /keyword_add."
                    )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_responses_guild_id "
                "ON responses(guild_id)"
            )
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
            # Per-guild joke scheduling. Each guild that runs /joke_activation
            # gets one row; missing row = no joke is sent for that guild.
            # `last_sent_date` is the ISO date of the last successful send so
            # the 30-second check loop fires exactly once per day per guild
            # within the configured time window.
            c.execute("""
                CREATE TABLE IF NOT EXISTS guild_joke_config (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    send_time TEXT NOT NULL,
                    last_sent_date TEXT
                )
            """)
            # Per-guild record of which jokes have been sent in that guild.
            # Preserves the "no repeats until pool exhausts" semantic per
            # guild — the same joke can run in different guilds across time
            # without one stealing it from the other. ON DELETE CASCADE on
            # joke_id cleans up rows when /jokes/<id> DELETE is called.
            c.execute("""
                CREATE TABLE IF NOT EXISTS guild_joke_sent (
                    guild_id INTEGER NOT NULL,
                    joke_id INTEGER NOT NULL,
                    sent_at REAL NOT NULL,
                    PRIMARY KEY (guild_id, joke_id),
                    FOREIGN KEY (joke_id) REFERENCES jokes(id) ON DELETE CASCADE
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
            # Alert state used by the LOW / HIGH buy-signal DMs in
            # `_process_scrape_item`. `last_alert_kind` is "low" / "high" /
            # NULL; `last_alert_price` records the price at the time of the
            # alert so the LOW re-alert threshold (≥1% lower than the
            # previous alert) can be evaluated without re-scanning history.
            if "last_alert_kind" not in columns:
                c.execute("ALTER TABLE scraped_items ADD COLUMN last_alert_kind TEXT")
            if "last_alert_price" not in columns:
                c.execute("ALTER TABLE scraped_items ADD COLUMN last_alert_price REAL")

            c.execute("""
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    price REAL,
                    timestamp REAL NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES scraped_items(id) ON DELETE CASCADE
                )
            """)
            # Indexes for the two access patterns that grow with retention:
            #   - `get_price_history` filters by item_id (via JOIN) and orders
            #     by timestamp. Without an index this becomes a full table
            #     scan as the table grows toward ~36k rows at 6-month retention.
            #   - `clean_old_price_history` does `WHERE timestamp < ?` every
            #     12 h, which also benefits from the timestamp index.
            # Both are `IF NOT EXISTS` so they're free to re-run on each boot.
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_price_history_item_id "
                "ON price_history(item_id)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_price_history_timestamp "
                "ON price_history(timestamp)"
            )
            # Migration: older builds stored rates relative to DKK
            # (`rate_to_dkk` column). We now pivot through EUR — drop the
            # legacy table so the daily refresh re-populates it with the
            # new semantics. Safe because `exchange_rates` is a
            # regenerable cache, not source-of-truth data.
            c.execute("PRAGMA table_info(exchange_rates)")
            existing_cols = {row[1] for row in c.fetchall()}
            if existing_cols and "rate_to_eur" not in existing_cols:
                logger.info(
                    "Migrating exchange_rates table from DKK-pivoted to "
                    "EUR-pivoted schema (dropping cache for repopulation)."
                )
                c.execute("DROP TABLE exchange_rates")
            c.execute("""
                CREATE TABLE IF NOT EXISTS exchange_rates (
                    currency TEXT PRIMARY KEY,
                    rate_to_eur REAL NOT NULL,
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
# `get_all_responses(guild_id)` runs on EVERY message the bot sees in that
# guild. We memoise per guild for a short TTL and invalidate explicitly
# whenever *this process* mutates that guild's rows (add/remove).
# Cross-process mutations (e.g. via the Flask API) become visible at most
# TTL seconds later.

_RESPONSES_CACHE_TTL = 30.0  # seconds
_responses_cache: dict[int, dict] = {}
_responses_cache_at: dict[int, float] = {}


def _invalidate_responses_cache(guild_id: int | None = None):
    global _responses_cache, _responses_cache_at
    if guild_id is None:
        _responses_cache = {}
        _responses_cache_at = {}
    else:
        _responses_cache.pop(guild_id, None)
        _responses_cache_at.pop(guild_id, None)


def add_response(keyword, response, guild_id: int):
    try:
        with _connect(commit=True) as c:
            c.execute(
                "INSERT INTO responses (guild_id, keyword, response) VALUES (?, ?, ?)",
                (guild_id, keyword.lower(), response),
            )
        logger.info(
            f"Inserted new response for keyword '{keyword}' in guild {guild_id}"
        )
        _invalidate_responses_cache(guild_id)
    except Exception:
        logger.exception(f"Failed to add response for '{keyword}' in guild {guild_id}")


def remove_response(keyword, guild_id: int, response=None):
    try:
        with _connect(commit=True) as c:
            if response is None:
                c.execute(
                    "DELETE FROM responses WHERE guild_id = ? AND keyword = ?",
                    (guild_id, keyword.lower()),
                )
            else:
                c.execute(
                    "DELETE FROM responses WHERE guild_id = ? AND keyword = ? AND response = ?",
                    (guild_id, keyword.lower(), response),
                )
            deleted = c.rowcount
        logger.info(
            f"Deleted {deleted} response(s) for keyword '{keyword}' in guild {guild_id}"
        )
        if deleted:
            _invalidate_responses_cache(guild_id)
        return deleted > 0
    except Exception:
        logger.exception(
            f"Failed to remove response for '{keyword}' in guild {guild_id}"
        )
        return False


def get_all_responses(guild_id: int):
    global _responses_cache, _responses_cache_at
    now = time.time()
    cached_at = _responses_cache_at.get(guild_id)
    if (
        guild_id in _responses_cache
        and cached_at is not None
        and (now - cached_at) < _RESPONSES_CACHE_TTL
    ):
        return _responses_cache[guild_id]
    try:
        with _connect() as c:
            c.execute(
                "SELECT keyword, response FROM responses WHERE guild_id = ?",
                (guild_id,),
            )
            data = c.fetchall()
        result: dict = {}
        for keyword, response in data:
            result.setdefault(keyword.lower(), []).append(response)
        _responses_cache[guild_id] = result
        _responses_cache_at[guild_id] = now
        return result
    except Exception:
        logger.exception(f"Failed to fetch responses for guild {guild_id}")
        return {}


def get_random_response(keyword, guild_id: int):
    try:
        with _connect() as c:
            c.execute(
                "SELECT response FROM responses WHERE guild_id = ? AND keyword = ?",
                (guild_id, keyword.lower()),
            )
            rows = c.fetchall()
        if not rows:
            return None
        return random.choice(rows)[0]
    except Exception:
        logger.exception(
            f"Error retrieving random response for '{keyword}' in guild {guild_id}"
        )
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


def get_unsent_joke_for_guild(guild_id):
    """Pick a random joke that hasn't been sent in this guild yet.

    Returns (joke_id, text) or None when the joke table is empty.
    Callers handle pool exhaustion explicitly via `reset_guild_joke_sent`
    + retry — keeps the no-result path unambiguous (truly no jokes vs.
    "all already sent in this guild")."""
    try:
        with _connect() as c:
            c.execute(
                """
                SELECT j.id, j.text
                FROM jokes j
                WHERE j.id NOT IN (
                    SELECT joke_id FROM guild_joke_sent WHERE guild_id = ?
                )
                """,
                (guild_id,),
            )
            rows = c.fetchall()
        if not rows:
            return None
        return random.choice(rows)
    except Exception:
        logger.exception(f"Failed to get unsent joke for guild {guild_id}")
        return None


def mark_guild_joke_sent(guild_id, joke_id):
    """Record that `joke_id` was sent in `guild_id`. UPSERT so a manual
    re-send (e.g. via the API) just refreshes the timestamp instead of
    raising a UNIQUE constraint."""
    try:
        with _connect(commit=True) as c:
            c.execute(
                "INSERT INTO guild_joke_sent (guild_id, joke_id, sent_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, joke_id) DO UPDATE SET sent_at = excluded.sent_at",
                (guild_id, joke_id, time.time()),
            )
        logger.debug(f"Marked joke {joke_id} as sent for guild {guild_id}")
    except Exception:
        logger.exception(f"Failed to mark joke {joke_id} as sent for guild {guild_id}")


def reset_guild_joke_sent(guild_id):
    """Wipe one guild's sent-joke history so its pool recycles."""
    try:
        with _connect(commit=True) as c:
            c.execute("DELETE FROM guild_joke_sent WHERE guild_id = ?", (guild_id,))
        logger.info(f"Reset joke sent history for guild {guild_id}")
    except Exception:
        logger.exception(f"Failed to reset joke history for guild {guild_id}")


def reset_all_guild_joke_sent():
    """Wipe every guild's sent-joke history. Used by `/jokes/reset` to
    give all subscribed guilds a fresh pool simultaneously."""
    try:
        with _connect(commit=True) as c:
            c.execute("DELETE FROM guild_joke_sent")
        logger.info("Reset joke sent history for all guilds")
    except Exception:
        logger.exception("Failed to reset joke history for all guilds")


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


def set_guild_joke_config(guild_id, channel_id, send_time):
    """Upsert one guild's joke schedule. Preserves `last_sent_date` on
    re-activation so a guild that re-runs /joke_activation on the same
    day doesn't get a duplicate joke."""
    try:
        with _connect(commit=True) as c:
            c.execute(
                """
                INSERT INTO guild_joke_config (guild_id, channel_id, send_time)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    send_time = excluded.send_time
                """,
                (guild_id, channel_id, send_time),
            )
        logger.info(
            f"Joke config set for guild {guild_id}: "
            f"channel={channel_id} time={send_time}"
        )
    except Exception:
        logger.exception(f"Failed to set joke config for guild {guild_id}")


def get_guild_joke_config(guild_id):
    """Return `{guild_id, channel_id, send_time, last_sent_date}` for a
    single guild, or None if no row exists."""
    try:
        with _connect() as c:
            c.execute(
                "SELECT guild_id, channel_id, send_time, last_sent_date "
                "FROM guild_joke_config WHERE guild_id = ?",
                (guild_id,),
            )
            row = c.fetchone()
        if not row:
            return None
        return {
            "guild_id": row[0],
            "channel_id": row[1],
            "send_time": row[2],
            "last_sent_date": row[3],
        }
    except Exception:
        logger.exception(f"Failed to fetch joke config for guild {guild_id}")
        return None


def get_all_guild_joke_configs():
    """Return all per-guild joke configs as a list of dicts. Used by
    the daily-joke check loop to iterate every subscribed guild."""
    try:
        with _connect() as c:
            c.execute(
                "SELECT guild_id, channel_id, send_time, last_sent_date "
                "FROM guild_joke_config"
            )
            rows = c.fetchall()
        return [
            {
                "guild_id": r[0],
                "channel_id": r[1],
                "send_time": r[2],
                "last_sent_date": r[3],
            }
            for r in rows
        ]
    except Exception:
        logger.exception("Failed to fetch all guild joke configs")
        # Return empty list (not None) so the check loop can iterate
        # unconditionally.
        return []


def clear_guild_joke_config(guild_id):
    """Remove a guild's joke schedule. Returns True if a row was
    deleted, False if no config existed. Does NOT touch
    `guild_joke_sent` history — re-activating later picks up where the
    old sent-set left off, preserving the no-repeats contract."""
    try:
        with _connect(commit=True) as c:
            c.execute("DELETE FROM guild_joke_config WHERE guild_id = ?", (guild_id,))
            deleted = c.rowcount
        if deleted:
            logger.info(f"Cleared joke config for guild {guild_id}")
        return deleted > 0
    except Exception:
        logger.exception(f"Failed to clear joke config for guild {guild_id}")
        return False


def set_guild_joke_last_sent(guild_id, date_iso):
    """Update `last_sent_date` for a guild. Assumes the row already
    exists (caller goes through `set_guild_joke_config` first)."""
    try:
        with _connect(commit=True) as c:
            c.execute(
                "UPDATE guild_joke_config SET last_sent_date = ? WHERE guild_id = ?",
                (date_iso, guild_id),
            )
    except Exception:
        logger.exception(f"Failed to set last_sent_date for guild {guild_id}")


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
    """Return every tracked item across all users.

    Tuple shape: `(id, user_id, url, last_price, last_stock_status, title,
    currency, last_alert_kind, last_alert_price)` — 9 fields. The two
    `last_alert_*` columns are the LOW/HIGH alert state used by the
    scrape loop's DM logic; they're NULL until an alert fires.
    """
    try:
        with _connect() as c:
            c.execute(
                "SELECT id, user_id, url, last_price, last_stock_status, title, "
                "currency, last_alert_kind, last_alert_price FROM scraped_items"
            )
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
    ~2.5 MB — well within SQLite and the Pi Zero W's resources.

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
