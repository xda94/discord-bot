"""Persistence for global per-user assistant preferences."""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from db.connection import _connect

logger = logging.getLogger("database")

_COLUMNS = (
    "language", "tone", "currency", "timezone", "notification_style", "llm_behavior"
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
        return get_assistant_profile(user_id)
    except Exception:
        logger.exception("Failed to update assistant profile for user %s", user_id)
        return None


def delete_assistant_profile(user_id: int) -> bool | None:
    try:
        with _connect(commit=True) as c:
            c.execute("DELETE FROM assistant_profiles WHERE user_id = ?", (user_id,))
            return c.rowcount > 0
    except Exception:
        logger.exception("Failed to reset assistant profile for user %s", user_id)
        return None


reset_assistant_profile = delete_assistant_profile
