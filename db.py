from __future__ import annotations

import hashlib
import os
import re
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
                        f"will not match until re-added per server with /keyword-add."
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
            # Per-guild joke scheduling. Each guild that runs /joke-activation
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
            # Per-item notification preferences. A target contains both a
            # number and currency so a user can, for example, track a Danish
            # shop but ask to be notified below 400 RON. `target_alerted`
            # makes the threshold a crossing alert rather than a 12-hour
            # reminder while the price remains below it.
            if "target_price" not in columns:
                c.execute("ALTER TABLE scraped_items ADD COLUMN target_price REAL")
            if "target_currency" not in columns:
                c.execute("ALTER TABLE scraped_items ADD COLUMN target_currency TEXT")
            if "target_alerted" not in columns:
                c.execute(
                    "ALTER TABLE scraped_items ADD COLUMN target_alerted "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            if "restock_only" not in columns:
                c.execute(
                    "ALTER TABLE scraped_items ADD COLUMN restock_only "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            # These fields are updated even for a failed attempt, allowing
            # `/wishlist-show` to distinguish fresh data from a blocked or
            # unsupported source without retaining page contents.
            if "last_checked_at" not in columns:
                c.execute("ALTER TABLE scraped_items ADD COLUMN last_checked_at REAL")
            if "last_check_status" not in columns:
                c.execute("ALTER TABLE scraped_items ADD COLUMN last_check_status TEXT")

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
            c.execute("""
                CREATE TABLE IF NOT EXISTS llm_response_feedback (
                    response_message_id INTEGER PRIMARY KEY,
                    requester_user_id INTEGER NOT NULL,
                    category TEXT NOT NULL CHECK (category IN ('mention', 'summon')),
                    guild_id INTEGER,
                    model TEXT,
                    prompt_version TEXT,
                    rating INTEGER CHECK (rating IN (-1, 1) OR rating IS NULL),
                    created_at REAL NOT NULL,
                    rated_at REAL
                )
            """)
            c.execute("PRAGMA table_info(llm_response_feedback)")
            feedback_columns = {row[1] for row in c.fetchall()}
            if "guild_id" not in feedback_columns:
                c.execute("ALTER TABLE llm_response_feedback ADD COLUMN guild_id INTEGER")
            if "model" not in feedback_columns:
                c.execute("ALTER TABLE llm_response_feedback ADD COLUMN model TEXT")
            if "prompt_version" not in feedback_columns:
                c.execute(
                    "ALTER TABLE llm_response_feedback ADD COLUMN prompt_version TEXT"
                )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_llm_feedback_category_rating "
                "ON llm_response_feedback(category, rating)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_llm_feedback_guild "
                "ON llm_response_feedback(guild_id, category, model, prompt_version)"
            )
            # Persistent conversational memory is opt-in at the channel level.
            # New user messages are retained only as pending observations until
            # the daily synthesis commits; durable storage contains the
            # synthesized entries rather than a conversation transcript.
            c.execute("""
                CREATE TABLE IF NOT EXISTS llm_memory_channels (
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (guild_id, channel_id)
                )
            """)
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_llm_memory_channels_guild "
                "ON llm_memory_channels(guild_id, enabled)"
            )
            # scope_id is the guild ID for server memory and 0 for the user's
            # separate DM profile. Discord snowflakes are always positive, so
            # zero cannot collide with a real guild.
            c.execute("""
                CREATE TABLE IF NOT EXISTS llm_user_memories (
                    scope_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    profile TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (scope_id, user_id)
                )
            """)
            memory_entries_sql = """
                CREATE TABLE {table_name} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    kind TEXT NOT NULL CHECK (
                        kind IN ('fact', 'impression', 'like', 'dislike', 'topic')
                    ),
                    content TEXT NOT NULL,
                    normalized_content TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(scope_id, user_id, kind, normalized_content)
                )
            """
            c.execute(memory_entries_sql.format(table_name="IF NOT EXISTS llm_memory_entries"))
            c.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' "
                "AND name = 'llm_memory_entries'"
            )
            memory_entries_definition = (c.fetchone() or ("",))[0] or ""
            if "'impression'" not in memory_entries_definition:
                # SQLite cannot widen a CHECK constraint in place. Rebuild the
                # table once while preserving stable IDs and timestamps.
                c.execute(
                    "ALTER TABLE llm_memory_entries "
                    "RENAME TO llm_memory_entries_legacy"
                )
                c.execute(memory_entries_sql.format(table_name="llm_memory_entries"))
                c.execute(
                    "INSERT INTO llm_memory_entries "
                    "(id, scope_id, user_id, kind, content, normalized_content, "
                    "source_text, created_at, updated_at) "
                    "SELECT id, scope_id, user_id, kind, content, normalized_content, "
                    "source_text, created_at, updated_at "
                    "FROM llm_memory_entries_legacy"
                )
                c.execute("DROP TABLE llm_memory_entries_legacy")
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_llm_memory_entries_owner "
                "ON llm_memory_entries(scope_id, user_id, updated_at DESC)"
            )
            c.execute("""
                CREATE TABLE IF NOT EXISTS llm_memory_transcript (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            # The transcript store was retired in favor of daily synthesis.
            # Clear legacy rows during migration so raw chat does not remain
            # after upgrading.
            c.execute("DELETE FROM llm_memory_transcript")
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_llm_memory_transcript_owner "
                "ON llm_memory_transcript(scope_id, user_id, created_at DESC)"
            )
            c.execute("""
                CREATE TABLE IF NOT EXISTS llm_memory_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    guild_id INTEGER,
                    channel_id INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_llm_memory_observations_owner "
                "ON llm_memory_observations(scope_id, user_id, created_at)"
            )
            # Migrate the previous one-blob profile format. The old row is
            # removed only after each complete fact has been copied, making
            # repeated startup safe through the unique normalized value.
            c.execute(
                "SELECT scope_id, user_id, profile, updated_at FROM llm_user_memories"
            )
            for scope_id, user_id, profile, updated_at in c.fetchall():
                heading = ""
                for raw_line in profile.splitlines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    if line.startswith("#"):
                        heading = line.lstrip("#").strip().rstrip(":")
                        continue
                    item_match = re.match(r"^(?:[-*+]|\d+[.)])\s+", line)
                    is_item = item_match is not None
                    fact = line[item_match.end():].strip() if item_match else line
                    if heading and is_item and not fact.casefold().startswith(
                        f"{heading}:".casefold()
                    ):
                        fact = f"{heading}: {fact}"
                    fact = " ".join(fact.split())
                    if not fact:
                        continue
                    c.execute(
                        "INSERT OR IGNORE INTO llm_memory_entries "
                        "(scope_id, user_id, kind, content, normalized_content, "
                        "source_text, created_at, updated_at) "
                        "VALUES (?, ?, 'fact', ?, ?, ?, ?, ?)",
                        (
                            scope_id,
                            user_id,
                            fact,
                            fact.casefold(),
                            "legacy-profile",
                            updated_at,
                            updated_at,
                        ),
                    )
                c.execute(
                    "DELETE FROM llm_user_memories WHERE scope_id = ? AND user_id = ?",
                    (scope_id, user_id),
                )
            # Preferences live separately from profiles so a server-wide
            # profile purge cannot silently opt users back in.
            c.execute("""
                CREATE TABLE IF NOT EXISTS llm_memory_preferences (
                    scope_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (scope_id, user_id)
                )
            """)
            # Migration from the decommissioned Amadeus integration. Those
            # client-id/secret pairs cannot be used with SerpApi, so remove the
            # obsolete credential table and require each user to log in again
            # with one SerpApi key. Flight trackers themselves are preserved.
            c.execute("PRAGMA table_info(flight_api_credentials)")
            credential_columns = {row[1] for row in c.fetchall()}
            if credential_columns and "api_key" not in credential_columns:
                c.execute("DROP TABLE flight_api_credentials")
                logger.info("Removed obsolete per-user Amadeus credentials.")

            # One SerpApi key per Discord user keeps account quotas isolated.
            # Keys are never returned by commands or written to logs. Operators
            # must restrict filesystem access to the SQLite database.
            c.execute("""
                CREATE TABLE IF NOT EXISTS flight_api_credentials (
                    user_id INTEGER PRIMARY KEY,
                    api_key TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            # Per-user fixed-date round-trip flight watches. ``trip_days`` is
            # retained only for compatibility with the short-lived flexible
            # tracker build; new trackers always store 0.
            c.execute("""
                CREATE TABLE IF NOT EXISTS flight_trackers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    origin TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    trip_days INTEGER NOT NULL DEFAULT 0,
                    adults INTEGER NOT NULL DEFAULT 1,
                    currency TEXT NOT NULL DEFAULT 'EUR',
                    last_price REAL,
                    last_departure_date TEXT,
                    last_return_date TEXT,
                    last_checked_at REAL,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    UNIQUE(
                        user_id, origin, destination, start_date, end_date,
                        trip_days, adults, currency
                    )
                )
            """)
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_flight_trackers_user_id "
                "ON flight_trackers(user_id)"
            )
            c.execute("""
                CREATE TABLE IF NOT EXISTS flight_price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tracker_id INTEGER NOT NULL,
                    price REAL NOT NULL,
                    currency TEXT NOT NULL,
                    departure_date TEXT NOT NULL,
                    return_date TEXT NOT NULL,
                    checked_at REAL NOT NULL,
                    FOREIGN KEY(tracker_id) REFERENCES flight_trackers(id) ON DELETE CASCADE
                )
            """)
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_flight_price_history_tracker_id "
                "ON flight_price_history(tracker_id)"
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
            # Per-guild switch for LLM inactivity nudges. Missing rows mean
            # enabled so existing installations preserve their current
            # behaviour after upgrading.
            c.execute("""
                CREATE TABLE IF NOT EXISTS guild_inactivity_config (
                    guild_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1))
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


# --- Persistent per-user LLM memory ---------------------------------------

def is_llm_memory_channel_enabled(guild_id, channel_id):
    """Return whether persistent memory is enabled in one guild channel."""
    try:
        with _connect() as c:
            c.execute(
                "SELECT enabled FROM llm_memory_channels "
                "WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            )
            row = c.fetchone()
        return bool(row[0]) if row else False
    except Exception:
        logger.exception(
            "Failed to read LLM memory channel setting for guild %s channel %s",
            guild_id,
            channel_id,
        )
        return False


def get_enabled_llm_memory_channels():
    """Return enabled (guild_id, channel_id) pairs for startup caching."""
    try:
        with _connect() as c:
            c.execute(
                "SELECT guild_id, channel_id FROM llm_memory_channels "
                "WHERE enabled = 1"
            )
            return c.fetchall()
    except Exception:
        logger.exception("Failed to list enabled LLM memory channels")
        return []


def set_llm_memory_channel_enabled(guild_id, channel_id, enabled):
    try:
        with _connect(commit=True) as c:
            c.execute(
                "INSERT INTO llm_memory_channels "
                "(guild_id, channel_id, enabled, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(guild_id, channel_id) DO UPDATE SET "
                "enabled = excluded.enabled, updated_at = excluded.updated_at",
                (guild_id, channel_id, int(bool(enabled)), time.time()),
            )
        return True
    except Exception:
        logger.exception(
            "Failed to update LLM memory channel setting for guild %s channel %s",
            guild_id,
            channel_id,
        )
        return False


def get_llm_user_memory(scope_id, user_id):
    try:
        with _connect() as c:
            c.execute(
                "SELECT profile FROM llm_user_memories "
                "WHERE scope_id = ? AND user_id = ?",
                (scope_id, user_id),
            )
            row = c.fetchone()
        return row[0] if row else None
    except Exception:
        logger.exception(
            "Failed to read LLM user memory for scope %s user %s",
            scope_id,
            user_id,
        )
        return None


def set_llm_user_memory(scope_id, user_id, profile):
    try:
        with _connect(commit=True) as c:
            c.execute(
                "INSERT INTO llm_user_memories "
                "(scope_id, user_id, profile, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(scope_id, user_id) DO UPDATE SET "
                "profile = excluded.profile, updated_at = excluded.updated_at",
                (scope_id, user_id, profile, time.time()),
            )
        return True
    except Exception:
        logger.exception(
            "Failed to save LLM user memory for scope %s user %s",
            scope_id,
            user_id,
        )
        return False


def delete_llm_user_memory(scope_id, user_id):
    try:
        with _connect(commit=True) as c:
            c.execute(
                "DELETE FROM llm_user_memories WHERE scope_id = ? AND user_id = ?",
                (scope_id, user_id),
            )
            removed = c.rowcount > 0
        return removed
    except Exception:
        logger.exception(
            "Failed to delete LLM user memory for scope %s user %s",
            scope_id,
            user_id,
        )
        return None


def purge_guild_llm_user_memories(guild_id):
    """Delete all saved memory for a guild while preserving preferences."""
    try:
        with _connect(commit=True) as c:
            c.execute(
                "SELECT COUNT(*) FROM ("
                "SELECT user_id FROM llm_user_memories WHERE scope_id = ? UNION "
                "SELECT user_id FROM llm_memory_entries WHERE scope_id = ? UNION "
                "SELECT user_id FROM llm_memory_transcript WHERE scope_id = ? UNION "
                "SELECT user_id FROM llm_memory_observations WHERE scope_id = ?)",
                (guild_id, guild_id, guild_id, guild_id),
            )
            removed = c.fetchone()[0]
            c.execute(
                "DELETE FROM llm_user_memories WHERE scope_id = ?",
                (guild_id,),
            )
            for table in (
                "llm_memory_entries",
                "llm_memory_transcript",
                "llm_memory_observations",
            ):
                c.execute(f"DELETE FROM {table} WHERE scope_id = ?", (guild_id,))
        return removed
    except Exception:
        logger.exception("Failed to purge LLM user memories for guild %s", guild_id)
        return None


def get_llm_memory_entries(scope_id, user_id):
    """Return stable memory rows, newest first."""
    try:
        with _connect() as c:
            c.execute(
                "SELECT id, kind, content, source_text, created_at, updated_at "
                "FROM llm_memory_entries WHERE scope_id = ? AND user_id = ? "
                "ORDER BY updated_at DESC, id DESC",
                (scope_id, user_id),
            )
            return c.fetchall()
    except Exception:
        logger.exception(
            "Failed to read memory entries for scope %s user %s", scope_id, user_id
        )
        return []


def add_llm_memory_transcript(
    scope_id,
    user_id,
    channel_id,
    role,
    content,
    *,
    created_at=None,
    max_messages=40,
    max_chars=16000,
    max_age_seconds=7 * 24 * 60 * 60,
):
    """Deprecated: raw conversation transcripts are no longer persisted."""
    return False


def get_llm_memory_transcript(
    scope_id, user_id, *, max_age_seconds=7 * 24 * 60 * 60, now=None
):
    """Return no rows and erase any transcript left by an older process."""
    try:
        with _connect(commit=True) as c:
            c.execute(
                "DELETE FROM llm_memory_transcript WHERE scope_id = ? AND user_id = ?",
                (scope_id, user_id),
            )
            return []
    except Exception:
        logger.exception(
            "Failed to read transcript for scope %s user %s", scope_id, user_id
        )
        return []


def add_llm_memory_observation(
    scope_id, user_id, guild_id, channel_id, content, *, created_at=None
):
    content = str(content).strip()
    if not content:
        return None
    try:
        with _connect(commit=True) as c:
            c.execute(
                "INSERT INTO llm_memory_observations "
                "(scope_id, user_id, guild_id, channel_id, content, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    scope_id,
                    user_id,
                    guild_id,
                    channel_id,
                    content,
                    time.time() if created_at is None else float(created_at),
                ),
            )
            return c.lastrowid
    except Exception:
        logger.exception(
            "Failed to append memory observation for scope %s user %s",
            scope_id,
            user_id,
        )
        return None


def get_llm_memory_observations(scope_id=None, user_id=None):
    try:
        with _connect() as c:
            query = (
                "SELECT id, scope_id, user_id, guild_id, channel_id, content, created_at "
                "FROM llm_memory_observations"
            )
            args = []
            clauses = []
            if scope_id is not None:
                clauses.append("scope_id = ?")
                args.append(scope_id)
            if user_id is not None:
                clauses.append("user_id = ?")
                args.append(user_id)
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY created_at, id"
            c.execute(query, args)
            return c.fetchall()
    except Exception:
        logger.exception("Failed to read pending memory observations")
        return []


def delete_llm_memory_observations(ids):
    ids = tuple(dict.fromkeys(int(value) for value in ids))
    if not ids:
        return True
    try:
        with _connect(commit=True) as c:
            placeholders = ",".join("?" for _ in ids)
            c.execute(
                "DELETE FROM llm_memory_observations WHERE id IN ("
                + placeholders
                + ")",
                ids,
            )
        return True
    except Exception:
        logger.exception("Failed to delete pending memory observations")
        return False


def delete_llm_memory_channel_observations(scope_id, channel_id):
    try:
        with _connect(commit=True) as c:
            c.execute(
                "DELETE FROM llm_memory_observations "
                "WHERE scope_id = ? AND channel_id = ?",
                (scope_id, channel_id),
            )
        return True
    except Exception:
        logger.exception("Failed to clear channel memory observations")
        return False


def apply_llm_memory_delta(scope_id, user_id, additions, corrections, observation_ids):
    """Atomically apply synthesized entries and delete their raw observations."""
    try:
        now = time.time()
        with _connect(commit=True) as c:
            for correction in corrections:
                entry_id = int(correction["id"])
                c.execute(
                    "SELECT id FROM llm_memory_entries "
                    "WHERE id = ? AND scope_id = ? AND user_id = ?",
                    (entry_id, scope_id, user_id),
                )
                if c.fetchone() is None:
                    raise ValueError(f"memory entry {entry_id} does not belong to user")
                kind = correction["kind"]
                content = " ".join(correction["content"].split())
                source_text = "sha256:" + hashlib.sha256(
                    correction["source_text"].encode("utf-8")
                ).hexdigest()
                normalized = content.casefold()
                c.execute(
                    "SELECT id FROM llm_memory_entries WHERE scope_id = ? "
                    "AND user_id = ? AND kind = ? AND normalized_content = ? "
                    "AND id != ?",
                    (scope_id, user_id, kind, normalized, entry_id),
                )
                duplicate = c.fetchone()
                if duplicate:
                    c.execute(
                        "DELETE FROM llm_memory_entries WHERE id = ?", (duplicate[0],)
                    )
                c.execute(
                    "UPDATE llm_memory_entries SET kind = ?, content = ?, "
                    "normalized_content = ?, source_text = ?, updated_at = ? "
                    "WHERE id = ?",
                    (kind, content, normalized, source_text, now, entry_id),
                )
            for addition in additions:
                kind = addition["kind"]
                content = " ".join(addition["content"].split())
                source_reference = "sha256:" + hashlib.sha256(
                    addition["source_text"].encode("utf-8")
                ).hexdigest()
                c.execute(
                    "INSERT OR IGNORE INTO llm_memory_entries "
                    "(scope_id, user_id, kind, content, normalized_content, "
                    "source_text, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        scope_id,
                        user_id,
                        kind,
                        content,
                        content.casefold(),
                        source_reference,
                        now,
                        now,
                    ),
                )
            ids = tuple(dict.fromkeys(int(value) for value in observation_ids))
            if ids:
                placeholders = ",".join("?" for _ in ids)
                c.execute(
                    "DELETE FROM llm_memory_observations WHERE scope_id = ? "
                    "AND user_id = ? AND id IN (" + placeholders + ")",
                    (scope_id, user_id, *ids),
                )
        return True
    except Exception:
        logger.exception(
            "Failed to apply memory delta for scope %s user %s", scope_id, user_id
        )
        return False


def delete_all_llm_user_memory(scope_id, user_id):
    """Delete legacy and current memory data for one user/scope."""
    try:
        with _connect(commit=True) as c:
            removed = 0
            for table in (
                "llm_user_memories",
                "llm_memory_entries",
                "llm_memory_transcript",
                "llm_memory_observations",
            ):
                c.execute(
                    f"DELETE FROM {table} WHERE scope_id = ? AND user_id = ?",
                    (scope_id, user_id),
                )
                removed += c.rowcount
        return removed
    except Exception:
        logger.exception(
            "Failed to delete all memory for scope %s user %s", scope_id, user_id
        )
        return None


def get_llm_memory_preference(scope_id, user_id):
    """Return True/False for an explicit preference, or None when unset."""
    try:
        with _connect() as c:
            c.execute(
                "SELECT enabled FROM llm_memory_preferences "
                "WHERE scope_id = ? AND user_id = ?",
                (scope_id, user_id),
            )
            row = c.fetchone()
        return bool(row[0]) if row else None
    except Exception:
        logger.exception(
            "Failed to read LLM memory preference for scope %s user %s",
            scope_id,
            user_id,
        )
        return None


def set_llm_memory_preference(scope_id, user_id, enabled):
    try:
        with _connect(commit=True) as c:
            c.execute(
                "INSERT INTO llm_memory_preferences "
                "(scope_id, user_id, enabled, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(scope_id, user_id) DO UPDATE SET "
                "enabled = excluded.enabled, updated_at = excluded.updated_at",
                (scope_id, user_id, int(bool(enabled)), time.time()),
            )
        return True
    except Exception:
        logger.exception(
            "Failed to update LLM memory preference for scope %s user %s",
            scope_id,
            user_id,
        )
        return False


def set_guild_joke_config(guild_id, channel_id, send_time):
    """Upsert one guild's joke schedule. Preserves `last_sent_date` on
    re-activation so a guild that re-runs /joke-activation on the same
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


# --- LLM response feedback -------------------------------------------------

def track_llm_response(
    message_id,
    requester_user_id,
    category,
    *,
    guild_id=None,
    model=None,
    prompt_version=None,
):
    """Register a rateable bot response without retaining its text/prompt."""
    if category not in ("mention", "summon"):
        raise ValueError(f"Unsupported LLM feedback category: {category!r}")
    try:
        with _connect(commit=True) as c:
            c.execute(
                "INSERT OR IGNORE INTO llm_response_feedback "
                "(response_message_id, requester_user_id, category, guild_id, model, "
                "prompt_version, rating, created_at) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    message_id, requester_user_id, category, guild_id, model,
                    prompt_version, time.time(),
                ),
            )
            return c.rowcount > 0
    except Exception:
        logger.exception(f"Failed to register LLM response feedback for message {message_id}")
        return False


def set_llm_response_rating(message_id, requester_user_id, rating):
    """Record the requester's latest thumbs-up/down for a bot response.

    The requester check prevents other channel members from skewing a user's
    private signal. Adding the opposite reaction later simply replaces the
    rating; we don't store reaction history.
    """
    if rating not in (-1, 1):
        raise ValueError("LLM feedback rating must be -1 or 1")
    try:
        with _connect(commit=True) as c:
            c.execute(
                "UPDATE llm_response_feedback SET rating = ?, rated_at = ? "
                "WHERE response_message_id = ? AND requester_user_id = ?",
                (rating, time.time(), message_id, requester_user_id),
            )
            return c.rowcount > 0
    except Exception:
        logger.exception(f"Failed to record LLM feedback for message {message_id}")
        return False


def get_llm_response_feedback(message_id):
    """Return one compact feedback row for diagnostics/tests, never text."""
    try:
        with _connect() as c:
            c.execute(
                "SELECT requester_user_id, category, rating, guild_id, model, "
                "prompt_version, created_at, rated_at "
                "FROM llm_response_feedback WHERE response_message_id = ?",
                (message_id,),
            )
            return c.fetchone()
    except Exception:
        logger.exception(f"Failed to read LLM feedback for message {message_id}")
        return None


def get_llm_feedback_summary(guild_id):
    """Aggregate a guild's rated replies without exposing prompts or text."""
    try:
        with _connect() as c:
            c.execute(
                "SELECT category, COALESCE(model, 'unknown'), "
                "COALESCE(prompt_version, 'unknown'), COUNT(*), "
                "SUM(CASE WHEN rating = 1 THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN rating = -1 THEN 1 ELSE 0 END) "
                "FROM llm_response_feedback WHERE guild_id = ? "
                "AND rating IS NOT NULL "
                "GROUP BY category, model, prompt_version "
                "ORDER BY category, model, prompt_version",
                (guild_id,),
            )
            return c.fetchall()
    except Exception:
        logger.exception(f"Failed to summarize LLM feedback for guild {guild_id}")
        return []


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


# --- Flight tracker functions ---

def set_flight_api_credentials(user_id, api_key):
    """Create or replace one Discord user's SerpApi key."""
    try:
        with _connect(commit=True) as c:
            c.execute(
                "INSERT INTO flight_api_credentials "
                "(user_id, api_key, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "api_key = excluded.api_key, "
                "updated_at = excluded.updated_at",
                (user_id, api_key, time.time()),
            )
        return True
    except Exception:
        logger.exception(f"Failed to store flight API credentials for user {user_id}")
        return False


def get_flight_api_credentials(user_id):
    """Return the user's private SerpApi key for internal provider use."""
    try:
        with _connect() as c:
            c.execute(
                "SELECT api_key, updated_at "
                "FROM flight_api_credentials WHERE user_id = ?",
                (user_id,),
            )
            row = c.fetchone()
            if row is None:
                return None
            return {
                "api_key": row[0],
                "updated_at": row[1],
            }
    except Exception:
        logger.exception(f"Failed to fetch flight API credentials for user {user_id}")
        return None


def delete_flight_api_credentials(user_id):
    try:
        with _connect(commit=True) as c:
            c.execute(
                "DELETE FROM flight_api_credentials WHERE user_id = ?", (user_id,)
            )
            return c.rowcount > 0
    except Exception:
        logger.exception(f"Failed to delete flight API credentials for user {user_id}")
        return False

_FLIGHT_TRACKER_COLUMNS = (
    "id, user_id, origin, destination, start_date, end_date, trip_days, "
    "adults, currency, last_price, last_departure_date, last_return_date, "
    "last_checked_at, last_error, created_at"
)


def _flight_tracker_dict(row):
    if row is None:
        return None
    keys = (
        "id", "user_id", "origin", "destination", "start_date", "end_date",
        "trip_days", "adults", "currency", "last_price", "last_departure_date",
        "last_return_date", "last_checked_at", "last_error", "created_at",
    )
    return dict(zip(keys, row))


def add_flight_tracker(
    user_id, origin, destination, start_date, end_date,
    trip_days=0, adults=1, currency="EUR",
):
    """Add one per-user route/date watch and return its numeric ID.

    The full search definition is unique per user. The same route can still be
    tracked for different periods, durations, passenger counts, or currencies.
    """
    try:
        with _connect(commit=True) as c:
            c.execute(
                "INSERT OR IGNORE INTO flight_trackers "
                "(user_id, origin, destination, start_date, end_date, trip_days, "
                "adults, currency, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id, origin, destination, start_date, end_date,
                    int(trip_days or 0), int(adults), currency, time.time(),
                ),
            )
            return c.lastrowid if c.rowcount > 0 else None
    except Exception:
        logger.exception(
            f"Failed to add flight tracker for user {user_id}: "
            f"{origin}-{destination}"
        )
        return None


def get_flight_tracker(tracker_id, user_id=None):
    try:
        with _connect() as c:
            query = f"SELECT {_FLIGHT_TRACKER_COLUMNS} FROM flight_trackers WHERE id = ?"
            params = [tracker_id]
            if user_id is not None:
                query += " AND user_id = ?"
                params.append(user_id)
            c.execute(query, params)
            return _flight_tracker_dict(c.fetchone())
    except Exception:
        logger.exception(f"Failed to fetch flight tracker {tracker_id}")
        return None


def get_user_flight_trackers(user_id):
    try:
        with _connect() as c:
            c.execute(
                f"SELECT {_FLIGHT_TRACKER_COLUMNS} FROM flight_trackers "
                "WHERE user_id = ? ORDER BY id",
                (user_id,),
            )
            return [_flight_tracker_dict(row) for row in c.fetchall()]
    except Exception:
        logger.exception(f"Failed to fetch flight trackers for user {user_id}")
        return []


def get_all_flight_trackers():
    try:
        with _connect() as c:
            c.execute(f"SELECT {_FLIGHT_TRACKER_COLUMNS} FROM flight_trackers ORDER BY id")
            return [_flight_tracker_dict(row) for row in c.fetchall()]
    except Exception:
        logger.exception("Failed to fetch flight trackers")
        return []


def delete_flight_tracker(user_id, tracker_id):
    try:
        with _connect(commit=True) as c:
            c.execute(
                "DELETE FROM flight_trackers WHERE id = ? AND user_id = ?",
                (tracker_id, user_id),
            )
            return c.rowcount > 0
    except Exception:
        logger.exception(f"Failed to delete flight tracker {tracker_id} for user {user_id}")
        return False


def update_flight_tracker_result(
    tracker_id, price=None, currency=None, departure_date=None,
    return_date=None, checked_at=None, error=None,
):
    """Persist a successful result or a failed check without losing good data."""
    try:
        with _connect(commit=True) as c:
            c.execute(
                "UPDATE flight_trackers SET "
                "last_price = COALESCE(?, last_price), "
                "currency = COALESCE(?, currency), "
                "last_departure_date = COALESCE(?, last_departure_date), "
                "last_return_date = COALESCE(?, last_return_date), "
                "last_checked_at = ?, last_error = ? WHERE id = ?",
                (
                    price, currency, departure_date, return_date,
                    checked_at if checked_at is not None else time.time(),
                    error, tracker_id,
                ),
            )
            return c.rowcount > 0
    except Exception:
        logger.exception(f"Failed to update flight tracker {tracker_id}")
        return False


def add_flight_price_history(
    tracker_id, price, currency, departure_date, return_date, checked_at=None,
):
    try:
        with _connect(commit=True) as c:
            c.execute(
                "INSERT INTO flight_price_history "
                "(tracker_id, price, currency, departure_date, return_date, checked_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    tracker_id, float(price), currency, departure_date, return_date,
                    checked_at if checked_at is not None else time.time(),
                ),
            )
    except Exception:
        logger.exception(f"Failed to add price history for flight tracker {tracker_id}")


def get_flight_price_history(tracker_id, user_id=None):
    try:
        with _connect() as c:
            query = (
                "SELECT h.price, h.currency, h.departure_date, h.return_date, h.checked_at "
                "FROM flight_price_history h JOIN flight_trackers t ON t.id = h.tracker_id "
                "WHERE h.tracker_id = ?"
            )
            params = [tracker_id]
            if user_id is not None:
                query += " AND t.user_id = ?"
                params.append(user_id)
            query += " ORDER BY h.checked_at"
            c.execute(query, params)
            return c.fetchall()
    except Exception:
        logger.exception(f"Failed to fetch flight price history for tracker {tracker_id}")
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


def set_guild_inactivity_enabled(guild_id: int, enabled: bool) -> None:
    """Persist whether LLM inactivity nudges may run in one guild."""
    try:
        with _connect(commit=True) as c:
            c.execute(
                "INSERT INTO guild_inactivity_config (guild_id, enabled) VALUES (?, ?) "
                "ON CONFLICT(guild_id) DO UPDATE SET enabled = excluded.enabled",
                (guild_id, int(enabled)),
            )
    except Exception:
        logger.exception(
            f"Failed to set inactivity configuration for guild {guild_id}"
        )


def is_guild_inactivity_enabled(guild_id: int) -> bool:
    """Return the guild setting; unconfigured guilds default to enabled."""
    try:
        with _connect() as c:
            c.execute(
                "SELECT enabled FROM guild_inactivity_config WHERE guild_id = ?",
                (guild_id,),
            )
            row = c.fetchone()
            return True if row is None else bool(row[0])
    except Exception:
        logger.exception(
            f"Failed to fetch inactivity configuration for guild {guild_id}"
        )
        # A database failure should not cause unsolicited messages.
        return False
