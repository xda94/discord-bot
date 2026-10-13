"""Database schema initialization and in-place migrations."""

from __future__ import annotations

import logging
import re
import sqlite3
import time

from db.connection import _connect
from db.constants import LLM_MEMORY_ENTRY_KINDS

logger = logging.getLogger("database")

def init_db():
    try:
        with _connect(commit=True) as c:
            deadline = time.monotonic() + 5.0
            while True:
                try:
                    c.execute("PRAGMA journal_mode = WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            c.execute("BEGIN IMMEDIATE")
            c.execute("""
                CREATE TABLE IF NOT EXISTS responses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    keyword TEXT NOT NULL,
                    response TEXT NOT NULL
                )
            """)
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
                CREATE TABLE IF NOT EXISTS birthdays (
                    user_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    guild_id INTEGER,
                    month INTEGER NOT NULL CHECK (month BETWEEN 1 AND 12),
                    day INTEGER NOT NULL CHECK (day BETWEEN 1 AND 31),
                    revision TEXT NOT NULL,
                    last_sent_year INTEGER
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
                CREATE TABLE IF NOT EXISTS guild_joke_config (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    send_time TEXT NOT NULL,
                    last_sent_date TEXT
                )
            """)
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
                CREATE TABLE IF NOT EXISTS assistant_profiles (
                    user_id INTEGER PRIMARY KEY,
                    language TEXT NOT NULL DEFAULT 'auto'
                        CHECK (language IN ('auto', 'en', 'ro')),
                    tone TEXT NOT NULL DEFAULT 'default'
                        CHECK (tone IN ('default', 'friendly', 'formal', 'playful')),
                    currency TEXT CHECK (currency IN ('RON', 'DKK', 'EUR', 'USD', 'GBP')),
                    timezone TEXT NOT NULL DEFAULT 'UTC',
                    notification_style TEXT NOT NULL DEFAULT 'standard'
                        CHECK (notification_style IN ('standard', 'compact')),
                    llm_behavior TEXT NOT NULL DEFAULT 'balanced'
                        CHECK (llm_behavior IN ('balanced', 'concise', 'detailed'))
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
            if "last_alert_kind" not in columns:
                c.execute("ALTER TABLE scraped_items ADD COLUMN last_alert_kind TEXT")
            if "last_alert_price" not in columns:
                c.execute("ALTER TABLE scraped_items ADD COLUMN last_alert_price REAL")
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
                        kind IN (
                            'fact', 'impression', 'like', 'dislike', 'topic',
                            'interest', 'opinion', 'other'
                        )
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
            if any(
                f"'{kind}'" not in memory_entries_definition
                for kind in LLM_MEMORY_ENTRY_KINDS
            ):
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
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_llm_memory_observations_channel "
                "ON llm_memory_observations(scope_id, channel_id, id)"
            )
            c.execute("""
                CREATE TABLE IF NOT EXISTS llm_memory_progress (
                    scope_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    last_completed_observation_id INTEGER NOT NULL DEFAULT 0,
                    active_cycle_endpoint_observation_id INTEGER,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (scope_id, channel_id)
                )
            """)
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
            c.execute("""
                CREATE TABLE IF NOT EXISTS llm_memory_preferences (
                    scope_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (scope_id, user_id)
                )
            """)
            c.execute("PRAGMA table_info(flight_api_credentials)")
            credential_columns = {row[1] for row in c.fetchall()}
            if credential_columns and "api_key" not in credential_columns:
                c.execute("DROP TABLE flight_api_credentials")
                logger.info("Removed obsolete per-user Amadeus credentials.")

            c.execute("""
                CREATE TABLE IF NOT EXISTS flight_api_credentials (
                    user_id INTEGER PRIMARY KEY,
                    api_key TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
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
            c.execute("""
                CREATE TABLE IF NOT EXISTS guild_activity (
                    guild_id INTEGER PRIMARY KEY,
                    last_time REAL NOT NULL,
                    channel_id INTEGER NOT NULL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS guild_inactivity_config (
                    guild_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1))
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS analytics_daily (
                    day TEXT NOT NULL,
                    category TEXT NOT NULL,
                    activity TEXT NOT NULL,
                    scope_type TEXT NOT NULL,
                    guild_id INTEGER NOT NULL DEFAULT 0,
                    count INTEGER NOT NULL DEFAULT 0,
                    latest_at REAL NOT NULL,
                    PRIMARY KEY (day, category, activity, scope_type, guild_id)
                )
            """)
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_analytics_daily_day_guild "
                "ON analytics_daily(day, guild_id)"
            )
            c.execute("""
                CREATE TABLE IF NOT EXISTS analytics_commands (
                    name TEXT PRIMARY KEY,
                    first_tracked_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS analytics_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            c.execute(
                "INSERT OR IGNORE INTO analytics_metadata (key, value) VALUES (?, ?)",
                ("tracking_started_at", str(time.time())),
            )
            from db.experience_schema import migrate
            migrate(c)
            from db import community_schema
            community_schema.migrate(c)
        logger.info("Database initialized.")
    except Exception:
        logger.exception("Critical error initializing database")
        raise
