"""Persistence operations for daily usage analytics."""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import db.connection as connection
from db.connection import _connect

logger = logging.getLogger("database")

# --- Deployer analytics ----------------------------------------------------

_ANALYTICS_CATEGORIES = {
    "command", "mention", "control", "automatic", "scheduled", "processing", "feedback",
    "failure",
}


def record_analytics_activity(
    category: str,
    activity: str,
    *,
    guild_id: int | None = None,
    scope_type: str | None = None,
    occurred_at: float | None = None,
) -> None:
    """Atomically increment one UTC daily counter.

    This deliberately uses a short SQLite timeout: analytics must never hold up
    a Discord response when the shared database is busy.
    """
    if category not in _ANALYTICS_CATEGORIES:
        raise ValueError(f"Unsupported analytics category: {category}")
    if not activity or not isinstance(activity, str):
        raise ValueError("Analytics activity must be a non-empty string")
    if scope_type is None:
        scope_type = "guild" if guild_id is not None else "global"
    if scope_type not in {"guild", "dm", "global"}:
        raise ValueError(f"Unsupported analytics scope: {scope_type}")
    if scope_type == "guild" and guild_id is None:
        raise ValueError("Guild analytics require guild_id")
    stored_guild_id = int(guild_id) if guild_id is not None else 0
    timestamp = time.time() if occurred_at is None else float(occurred_at)
    day = datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()
    conn = sqlite3.connect(connection.DB_FILE, timeout=0.25)
    try:
        conn.execute(
            """
            INSERT INTO analytics_daily
                (day, category, activity, scope_type, guild_id, count, latest_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(day, category, activity, scope_type, guild_id) DO UPDATE SET
                count = analytics_daily.count + 1,
                latest_at = MAX(analytics_daily.latest_at, excluded.latest_at)
            """,
            (day, category, activity, scope_type, stored_guild_id, timestamp),
        )
        conn.commit()
    finally:
        conn.close()


def refresh_analytics_command_catalog(names, *, observed_at: float | None = None) -> None:
    """Mark the current command set active while retaining removed commands."""
    timestamp = time.time() if observed_at is None else float(observed_at)
    cleaned = sorted({str(name).strip().lstrip("/") for name in names if str(name).strip()})
    conn = sqlite3.connect(connection.DB_FILE, timeout=0.25)
    try:
        conn.execute("UPDATE analytics_commands SET active = 0")
        for name in cleaned:
            conn.execute(
                """
                INSERT INTO analytics_commands
                    (name, first_tracked_at, last_seen_at, active)
                VALUES (?, ?, ?, 1)
                ON CONFLICT(name) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    active = 1
                """,
                (name, timestamp, timestamp),
            )
        conn.commit()
    finally:
        conn.close()


def get_analytics_summary(period="30d", guild_id=None, *, now: float | None = None):
    """Return the complete JSON-ready analytics report used by the API."""
    days_by_period = {"7d": 7, "30d": 30, "90d": 90}
    if period not in {*days_by_period, "all"}:
        raise ValueError("period must be one of: 7d, 30d, 90d, all")
    timestamp = time.time() if now is None else float(now)
    today = datetime.fromtimestamp(timestamp, timezone.utc).date()
    with _connect() as c:
        c.execute(
            "SELECT value FROM analytics_metadata WHERE key = 'tracking_started_at'"
        )
        row = c.fetchone()
        tracking_started_at = float(row[0]) if row else timestamp
        tracking_day = datetime.fromtimestamp(tracking_started_at, timezone.utc).date()
        requested_start = (
            tracking_day if period == "all" else today - timedelta(days=days_by_period[period] - 1)
        )
        start_day = max(tracking_day, requested_start)
        where = "day >= ? AND day <= ?"
        params: list = [start_day.isoformat(), today.isoformat()]
        if guild_id is not None:
            where += " AND scope_type = 'guild' AND guild_id = ?"
            params.append(int(guild_id))
        c.execute(
            f"SELECT day, category, activity, scope_type, SUM(count), MAX(latest_at) "
            f"FROM analytics_daily WHERE {where} "
            "GROUP BY day, category, activity, scope_type",
            params,
        )
        rows = c.fetchall()
        c.execute(
            "SELECT name, first_tracked_at, last_seen_at, active "
            "FROM analytics_commands ORDER BY name"
        )
        catalog = c.fetchall()

    category_totals = {name: 0 for name in sorted(_ANALYTICS_CATEGORIES)}
    scope_totals = {"guild": 0, "dm": 0, "global": 0}
    activity_totals: dict[tuple[str, str], dict] = {}
    daily_map = {}
    cursor = start_day
    while cursor <= today:
        daily_map[cursor.isoformat()] = {
            "date": cursor.isoformat(),
            "total": 0,
            **{name: 0 for name in sorted(_ANALYTICS_CATEGORIES)},
        }
        cursor += timedelta(days=1)
    for day, category, activity, scope, count, latest_at in rows:
        count = int(count)
        category_totals[category] += count
        scope_totals[scope] += count
        daily_map[day][category] += count
        daily_map[day]["total"] += count
        key = (category, activity)
        current = activity_totals.setdefault(
            key, {"category": category, "activity": activity, "count": 0, "latest_at": None}
        )
        current["count"] += count
        current["latest_at"] = max(current["latest_at"] or 0, latest_at)

    command_by_name = {
        activity: values for (category, activity), values in activity_totals.items()
        if category == "command"
    }
    commands = []
    catalog_names = set()
    for name, first_tracked_at, last_seen_at, active in catalog:
        catalog_names.add(name)
        values = command_by_name.get(name, {})
        commands.append({
            "name": name,
            "count": values.get("count", 0),
            "latest_at": values.get("latest_at"),
            "first_tracked_at": first_tracked_at,
            "last_seen_at": last_seen_at,
            "active": bool(active),
        })
    for name, values in command_by_name.items():
        if name not in catalog_names:
            commands.append({
                "name": name, "count": values["count"], "latest_at": values["latest_at"],
                "first_tracked_at": None, "last_seen_at": None, "active": False,
            })
    active_commands = sorted(
        (item for item in commands if item["active"]), key=lambda item: (-item["count"], item["name"])
    )
    inactive_commands = sorted(
        (item for item in commands if not item["active"]), key=lambda item: (-item["count"], item["name"])
    )
    features = sorted(
        (values for key, values in activity_totals.items() if key[0] not in {"command", "failure"}),
        key=lambda item: (item["category"], -item["count"], item["activity"]),
    )
    failures = sorted(
        (values for key, values in activity_totals.items() if key[0] == "failure"),
        key=lambda item: (-item["count"], item["activity"]),
    )
    return {
        "tracking_started_at": tracking_started_at,
        "tracking_started_date": tracking_day.isoformat(),
        "period": {"key": period, "start": start_day.isoformat(), "end": today.isoformat()},
        "scope": {"type": "guild" if guild_id is not None else "all", "guild_id": guild_id},
        "category_totals": category_totals,
        "scope_totals": scope_totals,
        "commands": {
            "active": active_commands,
            "inactive": inactive_commands,
            "most_used": active_commands,
            "least_used": sorted(active_commands, key=lambda item: (item["count"], item["name"])),
            "unused": [item for item in active_commands if item["count"] == 0],
        },
        "features": features,
        "failures": failures,
        "daily": list(daily_map.values()),
    }
