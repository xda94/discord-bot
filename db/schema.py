"""Database schema initialization and in-place migrations."""

from __future__ import annotations

import logging
import re
import time

from db.connection import _connect
from db.constants import LLM_MEMORY_ENTRY_KINDS

logger = logging.getLogger("database")

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
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_llm_memory_observations_channel "
                "ON llm_memory_observations(scope_id, channel_id, id)"
            )
            # A channel cycle is deliberately separate from user memory. The
            # checkpoint and endpoint survive a restart while source rows are
            # deleted one author-sized chunk at a time.
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
        logger.info("Database initialized.")
    except Exception:
        logger.exception("Critical error initializing database")

