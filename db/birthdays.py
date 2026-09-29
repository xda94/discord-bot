"""Persistence helpers for per-user recurring birthdays."""

from __future__ import annotations

import uuid
from datetime import date

from db.connection import _connect


def set_birthday(
    user_id: int,
    channel_id: int,
    guild_id: int | None,
    month: int,
    day: int,
) -> None:
    """Create or replace one user's birthday registration."""
    date(2000, month, day)
    revision = uuid.uuid4().hex
    with _connect(commit=True) as c:
        c.execute(
            """
            INSERT INTO birthdays
                (user_id, channel_id, guild_id, month, day, revision)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                guild_id = excluded.guild_id,
                month = excluded.month,
                day = excluded.day,
                revision = excluded.revision
            """,
            (user_id, channel_id, guild_id, month, day, revision),
        )


def get_birthday(user_id: int):
    """Return a user's registration without the user ID, or ``None``."""
    with _connect() as c:
        c.execute(
            """
            SELECT channel_id, guild_id, month, day, revision, last_sent_year
            FROM birthdays
            WHERE user_id = ?
            """,
            (user_id,),
        )
        return c.fetchone()


def delete_birthday(user_id: int) -> bool:
    """Delete a user's registration and report whether it existed."""
    with _connect(commit=True) as c:
        c.execute("DELETE FROM birthdays WHERE user_id = ?", (user_id,))
        return c.rowcount > 0


def get_due_birthdays(today: date):
    """Return registrations due on ``today`` and not sent in its year."""
    with _connect() as c:
        c.execute(
            """
            SELECT user_id, channel_id, guild_id, month, day, revision, last_sent_year
            FROM birthdays
            WHERE month = ?
              AND day = ?
              AND (last_sent_year IS NULL OR last_sent_year < ?)
            ORDER BY user_id
            """,
            (today.month, today.day, today.year),
        )
        return c.fetchall()


def mark_birthday_sent(user_id: int, revision: str, year: int) -> bool:
    """Advance a registration's delivery marker if its revision is current."""
    with _connect(commit=True) as c:
        c.execute(
            """
            UPDATE birthdays
            SET last_sent_year = ?
            WHERE user_id = ?
              AND revision = ?
              AND (last_sent_year IS NULL OR last_sent_year < ?)
            """,
            (year, user_id, revision, year),
        )
        return c.rowcount > 0
