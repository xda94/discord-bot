"""Persistence for global per-user assistant preferences."""

from __future__ import annotations

import logging
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from db.connection import _connect

logger = logging.getLogger("database")

_COLUMNS = (
    "language", "tone", "currency", "timezone", "notification_style", "llm_behavior", "timezone_configured", "quiet_start", "quiet_end", "delivery_mode", "digest_time"
)
_CHOICES = {
    "language": {"auto", "en", "ro"},
    "tone": {"default", "friendly", "formal", "playful"},
    "currency": {None, "RON", "DKK", "EUR", "USD", "GBP"},
    "notification_style": {"standard", "compact"},
    "llm_behavior": {"balanced", "concise", "detailed"},
}


def _as_dict(row):
    if row is None:
        return None
    return dict(zip(_COLUMNS, row))


def get_assistant_profile(user_id: int):
    """Read a stored profile without creating a default row."""
    try:
        with _connect() as c:
            c.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM assistant_profiles WHERE user_id = ?",
                (user_id,),
            )
            return _as_dict(c.fetchone())
    except Exception:
        logger.exception("Failed to read assistant profile for user %s", user_id)
        return None


def set_assistant_profile(user_id: int, **updates):
    """Create or partially update a profile, preserving unspecified values."""
    if not updates or set(updates) - set(_COLUMNS):
        return None
    try:
        from assistant_profiles import validate_profile_updates
        updates = validate_profile_updates(**updates)
        existing = get_assistant_profile(user_id) or {}
        if "timezone" in updates:
            updates["timezone_configured"] = True
        combined = {**existing, **updates}
        if bool(combined.get("quiet_start")) != bool(combined.get("quiet_end")):
            return None
        if combined.get("quiet_start") == combined.get("quiet_end") and combined.get("quiet_start"):
            return None
        if (combined.get("quiet_start") or combined.get("delivery_mode") == "daily") and not combined.get("timezone_configured"):
            return None
    except (ValueError, TypeError):
        return None
    for name, value in updates.items():
        if name in _CHOICES and value not in _CHOICES[name]:
            return None
        if name == "timezone":
            try:
                ZoneInfo(value)
            except (TypeError, ValueError, ZoneInfoNotFoundError):
                return None
    try:
        with _connect(commit=True) as c:
            c.execute(
                "INSERT OR IGNORE INTO assistant_profiles (user_id) VALUES (?)",
                (user_id,),
            )
            assignments = ", ".join(f"{name} = ?" for name in updates)
            c.execute(
                f"UPDATE assistant_profiles SET {assignments} WHERE user_id = ?",
                (*updates.values(), user_id),
            )
        saved = get_assistant_profile(user_id)
        if set(updates) & {'timezone', 'quiet_start', 'quiet_end', 'delivery_mode', 'digest_time'}:
            from assistant_profiles import effective_profile
            from notifications import delivery_at
            with _connect(commit=True) as c:
                c.execute("UPDATE notification_outbox SET due_at=? WHERE user_id=? AND state='pending' AND sent_parts=0", (delivery_at(effective_profile(saved)), user_id))
        return saved
    except Exception:
        logger.exception("Failed to update assistant profile for user %s", user_id)
        return None


def delete_assistant_profile(user_id: int) -> bool | None:
    try:
        with _connect(commit=True) as c:
            c.execute("DELETE FROM assistant_profiles WHERE user_id = ?", (user_id,))
            removed = c.rowcount > 0
            c.execute("UPDATE notification_outbox SET due_at=? WHERE user_id=? AND state='pending' AND sent_parts=0", (time.time(),user_id))
            return removed
    except Exception:
        logger.exception("Failed to reset assistant profile for user %s", user_id)
        return None


reset_assistant_profile = delete_assistant_profile
