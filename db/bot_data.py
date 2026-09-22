"""Persistence for core bot data and small feature domains."""

from __future__ import annotations

import logging
import random
import time

from db.connection import _connect

logger = logging.getLogger("database")

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

