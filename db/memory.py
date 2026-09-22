"""Persistence operations for conversational memory."""

from __future__ import annotations

import hashlib
import logging
import time

from db.connection import _connect
from db.constants import LLM_MEMORY_ENTRY_KINDS

logger = logging.getLogger("database")

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
            if not enabled:
                c.execute(
                    "DELETE FROM llm_memory_progress "
                    "WHERE scope_id = ? AND channel_id = ?",
                    (guild_id, channel_id),
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
            c.execute(
                "DELETE FROM llm_memory_progress WHERE scope_id = ?",
                (guild_id,),
            )
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


def add_manual_llm_memory_entry(scope_id, user_id, content, *, kind="fact"):
    """Add one categorized user-authored memory.

    Return True when inserted, False when the normalized entry already exists,
    and None when the database write fails.
    """
    content = " ".join(str(content).split())
    if not content or kind not in LLM_MEMORY_ENTRY_KINDS:
        return False
    normalized = content.casefold()
    source_reference = "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()
    try:
        now = time.time()
        with _connect(commit=True) as c:
            c.execute(
                "INSERT OR IGNORE INTO llm_memory_entries "
                "(scope_id, user_id, kind, content, normalized_content, "
                "source_text, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    scope_id,
                    user_id,
                    kind,
                    content,
                    normalized,
                    source_reference,
                    now,
                    now,
                ),
            )
            return c.rowcount == 1
    except Exception:
        logger.exception(
            "Failed to add manual memory entry for scope %s user %s",
            scope_id,
            user_id,
        )
        return None


def delete_llm_memory_entries_by_text(scope_id, user_id, content):
    """Delete owned entries matching normalized full text and return the count."""
    normalized = " ".join(str(content).split()).casefold()
    if not normalized:
        return 0
    try:
        with _connect(commit=True) as c:
            c.execute(
                "DELETE FROM llm_memory_entries WHERE scope_id = ? AND user_id = ? "
                "AND normalized_content = ?",
                (scope_id, user_id, normalized),
            )
            return c.rowcount
    except Exception:
        logger.exception(
            "Failed to delete memory entries by text for scope %s user %s",
            scope_id,
            user_id,
        )
        return None


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


def get_llm_memory_observation_channels():
    """Return scope/channel pairs that have captured observations."""
    try:
        with _connect() as c:
            c.execute(
                "SELECT DISTINCT scope_id, channel_id "
                "FROM llm_memory_observations"
            )
            return c.fetchall()
    except Exception:
        logger.exception("Failed to list memory observation channels")
        return []


def get_llm_memory_channel_observations(
    scope_id,
    channel_id,
    *,
    after_id=0,
    through_id=None,
    max_created_at=None,
):
    """Return captured observations for one channel in observation order."""
    try:
        with _connect() as c:
            query = (
                "SELECT id, scope_id, user_id, guild_id, channel_id, "
                "content, created_at FROM llm_memory_observations "
                "WHERE scope_id = ? AND channel_id = ? AND id > ?"
            )
            args = [scope_id, channel_id, after_id]
            if through_id is not None:
                query += " AND id <= ?"
                args.append(through_id)
            if max_created_at is not None:
                query += " AND created_at <= ?"
                args.append(max_created_at)
            query += " ORDER BY id"
            c.execute(query, args)
            return c.fetchall()
    except Exception:
        logger.exception(
            "Failed to read memory observations for scope %s channel %s",
            scope_id,
            channel_id,
        )
        return []


def get_llm_memory_progress(scope_id, channel_id):
    """Return ``(checkpoint, active_endpoint)`` for a channel."""
    try:
        with _connect() as c:
            c.execute(
                "SELECT last_completed_observation_id, "
                "active_cycle_endpoint_observation_id "
                "FROM llm_memory_progress WHERE scope_id = ? AND channel_id = ?",
                (scope_id, channel_id),
            )
            row = c.fetchone()
        return (0, None) if row is None else (row[0], row[1])
    except Exception:
        logger.exception(
            "Failed to read memory progress for scope %s channel %s",
            scope_id,
            channel_id,
        )
        return (0, None)


def begin_llm_memory_cycle(scope_id, channel_id, endpoint_observation_id):
    """Persist a fixed endpoint if the channel has no active cycle."""
    try:
        with _connect(commit=True) as c:
            now = time.time()
            c.execute(
                "INSERT INTO llm_memory_progress "
                "(scope_id, channel_id, last_completed_observation_id, "
                "active_cycle_endpoint_observation_id, updated_at) "
                "VALUES (?, ?, 0, ?, ?) "
                "ON CONFLICT(scope_id, channel_id) DO UPDATE SET "
                "active_cycle_endpoint_observation_id = excluded."
                "active_cycle_endpoint_observation_id, updated_at = excluded.updated_at "
                "WHERE llm_memory_progress.active_cycle_endpoint_observation_id IS NULL",
                (scope_id, channel_id, int(endpoint_observation_id), now),
            )
            return c.rowcount > 0
    except Exception:
        logger.exception(
            "Failed to start memory cycle for scope %s channel %s",
            scope_id,
            channel_id,
        )
        return False


def complete_llm_memory_cycle_if_empty(scope_id, channel_id):
    """Advance a channel checkpoint only after its active cycle is empty."""
    try:
        with _connect(commit=True) as c:
            c.execute(
                "SELECT last_completed_observation_id, "
                "active_cycle_endpoint_observation_id "
                "FROM llm_memory_progress WHERE scope_id = ? AND channel_id = ?",
                (scope_id, channel_id),
            )
            progress = c.fetchone()
            if progress is None or progress[1] is None:
                return False
            checkpoint, endpoint = progress
            c.execute(
                "SELECT 1 FROM llm_memory_observations "
                "WHERE scope_id = ? AND channel_id = ? "
                "AND id > ? AND id <= ? LIMIT 1",
                (scope_id, channel_id, checkpoint, endpoint),
            )
            if c.fetchone() is not None:
                return False
            c.execute(
                "UPDATE llm_memory_progress SET "
                "last_completed_observation_id = ?, "
                "active_cycle_endpoint_observation_id = NULL, updated_at = ? "
                "WHERE scope_id = ? AND channel_id = ? "
                "AND active_cycle_endpoint_observation_id = ?",
                (endpoint, time.time(), scope_id, channel_id, endpoint),
            )
            return c.rowcount > 0
    except Exception:
        logger.exception(
            "Failed to complete memory cycle for scope %s channel %s",
            scope_id,
            channel_id,
        )
        return False


def clear_llm_memory_progress(scope_id, channel_id):
    """Clear a channel checkpoint and any active cycle."""
    try:
        with _connect(commit=True) as c:
            c.execute(
                "DELETE FROM llm_memory_progress "
                "WHERE scope_id = ? AND channel_id = ?",
                (scope_id, channel_id),
            )
        return True
    except Exception:
        logger.exception(
            "Failed to clear memory progress for scope %s channel %s",
            scope_id,
            channel_id,
        )
        return False


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
            c.execute(
                "DELETE FROM llm_memory_progress "
                "WHERE scope_id = ? AND channel_id = ?",
                (scope_id, channel_id),
            )
        return True
    except Exception:
        logger.exception("Failed to clear channel memory observations")
        return False


def apply_llm_memory_delta(
    scope_id,
    user_id,
    additions,
    corrections,
    observation_ids,
    *,
    channel_id=None,
    cycle_endpoint_observation_id=None,
):
    """Atomically apply synthesized entries and delete their raw observations."""
    try:
        now = time.time()
        with _connect(commit=True) as c:
            progress = None
            if channel_id is not None and cycle_endpoint_observation_id is not None:
                c.execute(
                    "SELECT last_completed_observation_id, "
                    "active_cycle_endpoint_observation_id "
                    "FROM llm_memory_progress "
                    "WHERE scope_id = ? AND channel_id = ?",
                    (scope_id, channel_id),
                )
                progress = c.fetchone()
                if progress is None or progress[1] != cycle_endpoint_observation_id:
                    raise ValueError("memory cycle is no longer active")
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
                if c.rowcount != len(ids):
                    raise ValueError("memory observations changed during commit")
            if progress is not None:
                checkpoint, endpoint = progress
                c.execute(
                    "SELECT 1 FROM llm_memory_observations "
                    "WHERE scope_id = ? AND channel_id = ? "
                    "AND id > ? AND id <= ? LIMIT 1",
                    (scope_id, channel_id, checkpoint, endpoint),
                )
                if c.fetchone() is None:
                    c.execute(
                        "UPDATE llm_memory_progress SET "
                        "last_completed_observation_id = ?, "
                        "active_cycle_endpoint_observation_id = NULL, "
                        "updated_at = ? WHERE scope_id = ? AND channel_id = ? "
                        "AND active_cycle_endpoint_observation_id = ?",
                        (
                            endpoint,
                            now,
                            scope_id,
                            channel_id,
                            endpoint,
                        ),
                    )
                    if c.rowcount != 1:
                        raise ValueError("memory cycle changed during commit")
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

