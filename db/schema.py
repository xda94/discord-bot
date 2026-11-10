"""Database schema initialization and in-place migrations."""

from __future__ import annotations

import logging
import sqlite3
import time

from db.connection import _connect

logger = logging.getLogger("database")

REMOVED_TABLES = (
    "llm_response_feedback", "llm_memory_channels", "llm_user_memories",
    "llm_memory_entries", "llm_memory_transcript", "llm_memory_observations",
    "llm_memory_progress", "llm_memory_preferences",
)

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
                        "%s legacy keyword(s) have no guild_id and "
                        "will not match until re-added per server with /keyword-add.",
                        legacy_count,
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
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_keyword_usage_guild_keyword "
                "ON keyword_usage(guild_id, keyword)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_keyword_usage_guild_user "
                "ON keyword_usage(guild_id, user_id)"
            )
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
                "CREATE INDEX IF NOT EXISTS idx_price_history_item_ts "
                "ON price_history(item_id, timestamp)"
            )
            c.execute("DROP INDEX IF EXISTS idx_price_history_item_id")
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_price_history_timestamp "
                "ON price_history(timestamp)"
            )
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
            c.execute("""
                CREATE TABLE IF NOT EXISTS feature_interactions (
                    user_id INTEGER NOT NULL,
                    feature TEXT NOT NULL CHECK (feature IN ('wishlist', 'flights')),
                    scope_type TEXT NOT NULL CHECK (scope_type IN ('guild', 'dm')),
                    guild_id INTEGER NOT NULL DEFAULT 0,
                    user_name TEXT NOT NULL DEFAULT '',
                    guild_name TEXT NOT NULL DEFAULT '',
                    count INTEGER NOT NULL DEFAULT 0,
                    first_at REAL NOT NULL,
                    last_at REAL NOT NULL,
                    last_action TEXT NOT NULL,
                    PRIMARY KEY (user_id, feature, scope_type, guild_id)
                )
            """)
            for table in REMOVED_TABLES:
                c.execute(f"DROP TABLE IF EXISTS {table}")
            from db.experience_schema import migrate
            migrate(c)
            from db import community_schema
            community_schema.migrate(c)
        logger.info("Database initialized.")
    except Exception:
        logger.exception("Critical error initializing database")
        raise
