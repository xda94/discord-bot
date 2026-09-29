"""Atomic persistence for the global sponsor-tier catalog."""

from __future__ import annotations

import sqlite3
import uuid
from copy import deepcopy
from typing import Any

from db import connection
from sponsor_tiers import (
    DEFAULT_SPONSOR_TIERS,
    DuplicateTierNameError,
    UnknownTierError,
    encode_tier_catalog,
    normalized_tier_name,
    validate_catalog,
    validate_tier,
)


SETTING_KEY = "sponsor_tiers"


def _read_catalog_value(cursor) -> dict[str, dict[str, Any]]:
    cursor.execute("SELECT value FROM settings WHERE key = ?", (SETTING_KEY,))
    row = cursor.fetchone()
    if row is None:
        return deepcopy(DEFAULT_SPONSOR_TIERS)
    # decode_tier_catalog is deliberately called here rather than replacing a
    # malformed value with defaults. Operators need to see and repair corrupt
    # configuration instead of receiving a successful but misleading write.
    from sponsor_tiers import decode_tier_catalog

    return decode_tier_catalog(row[0])


def get_sponsor_tiers() -> dict[str, dict[str, Any]]:
    """Return a fresh effective catalog, without seeding missing settings."""
    conn = sqlite3.connect(connection.DB_FILE, timeout=30)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        cursor = conn.cursor()
        return _read_catalog_value(cursor)
    finally:
        conn.close()


def _encode_saved_catalog(catalog: dict[str, dict[str, Any]]) -> str:
    return encode_tier_catalog(catalog)


def save_sponsor_tier(
    tier_id: str | None,
    name: Any,
    price_per_year: Any,
    chance: Any,
) -> dict[str, Any]:
    """Create or update one tier and return it only after the commit succeeds."""
    conn = sqlite3.connect(connection.DB_FILE, timeout=30)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        # Serialize read/merge/write cycles across the bot and Flask process.
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        catalog = _read_catalog_value(cursor)

        if tier_id is None:
            new_id = f"tier-{uuid.uuid4()}"
            while new_id in catalog:
                new_id = f"tier-{uuid.uuid4()}"
            tier_id = new_id
        elif tier_id not in catalog:
            raise UnknownTierError(f"unknown sponsor tier: {tier_id}")

        tier = validate_tier(tier_id, name, price_per_year, chance)
        candidate_name = normalized_tier_name(tier["name"])
        for existing_id, existing in catalog.items():
            if existing_id != tier_id and normalized_tier_name(existing["name"]) == candidate_name:
                raise DuplicateTierNameError(
                    f"tier name duplicates existing tier {existing_id}"
                )

        catalog[tier_id] = tier
        # Revalidate the merged catalog before writing, including duplicate
        # names in any pre-existing custom/default data.
        catalog = validate_catalog(catalog)
        payload = _encode_saved_catalog(catalog)
        cursor.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SETTING_KEY, payload),
        )
        conn.commit()
        return deepcopy(catalog[tier_id])
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
