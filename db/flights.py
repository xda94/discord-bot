"""Persistence operations for flight credentials and trackers."""

from __future__ import annotations

import logging
import time

from db.connection import _connect

logger = logging.getLogger("database")

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

