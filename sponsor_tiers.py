"""Shared validation and formatting for the global sponsor-tier catalog."""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


class SponsorTierError(ValueError):
    """Base class for invalid or unusable sponsor-tier configuration."""


class TierValidationError(SponsorTierError):
    """A tier field or record is invalid."""


class DuplicateTierNameError(SponsorTierError):
    """Two tiers would have the same normalized display name."""


class UnknownTierError(SponsorTierError):
    """A requested tier ID does not exist in the current catalog."""


class TierCatalogError(SponsorTierError):
    """Persisted tier catalog data is malformed or unsupported."""


CATALOG_VERSION = 1
_TIER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_CUSTOM_TIER_ID_RE = re.compile(r"^tier-[0-9a-f-]{36}$")


# Keep these values in insertion order: it is the public plan-list order.
DEFAULT_SPONSOR_TIERS = {
    "standard": {
        "id": "standard",
        "name": "Sponsor Standard",
        "price_per_year": "6",
        "chance": 0.01,
    },
    "entuziast": {
        "id": "entuziast",
        "name": "Sponsor Entuziast",
        "price_per_year": "8",
        "chance": 0.03,
    },
    "premium": {
        "id": "premium",
        "name": "Sponsor Premium",
        "price_per_year": "10",
        "chance": 0.05,
    },
    "ultra": {
        "id": "ultra",
        "name": "Sponsor Ultra Pro Max",
        "price_per_year": "20",
        "chance": 0.08,
    },
}


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise TierValidationError(f"{field} must be a number")
    try:
        if isinstance(value, float) and not math.isfinite(value):
            raise InvalidOperation
        result = Decimal(str(value).strip()) if isinstance(value, str) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise TierValidationError(f"{field} must be a finite number") from None
    if not result.is_finite():
        raise TierValidationError(f"{field} must be a finite number")
    return result


def _canonical_price(value: Any) -> str:
    amount = _decimal(value, "price_per_year")
    if amount < 0:
        raise TierValidationError("price_per_year must be nonnegative")
    if amount.as_tuple().exponent < -2:
        raise TierValidationError("price_per_year may have at most two decimal places")
    # Fixed-point formatting avoids persisting scientific notation while
    # retaining meaningful decimal places such as 12.50.
    return format(amount, "f")


def _canonical_chance(value: Any) -> float:
    chance = _decimal(value, "chance")
    if chance < 0 or chance > 1:
        raise TierValidationError("chance must be between 0 and 1")
    result = float(chance)
    if not math.isfinite(result):
        raise TierValidationError("chance must be a finite number")
    return result


def normalized_tier_name(name: Any) -> str:
    """Return the comparison key used for duplicate-name checks."""
    if not isinstance(name, str):
        raise TierValidationError("name must be a string")
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise TierValidationError("name cannot contain control characters")
    display_name = name.strip()
    if not display_name:
        raise TierValidationError("name cannot be empty")
    if len(display_name) > 80:
        raise TierValidationError("name must be at most 80 characters")
    return " ".join(display_name.split()).casefold()


def validate_tier(
    tier_id: Any,
    name: Any = None,
    price_per_year: Any = None,
    chance: Any = None,
) -> dict[str, Any]:
    """Validate and canonicalize one tier.

    The first argument may also be a complete record, which keeps this helper
    convenient for decoding JSON while retaining the explicit four-argument
    form for API and database callers.
    """
    if isinstance(tier_id, Mapping) and name is None and price_per_year is None and chance is None:
        record = dict(tier_id)
        tier_id = record.get("id")
        name = record.get("name")
        price_per_year = record.get("price_per_year")
        chance = record.get("chance")
        if set(record) != {"id", "name", "price_per_year", "chance"}:
            raise TierValidationError(
                "tier records must contain only id, name, price_per_year, and chance"
            )

    if not isinstance(tier_id, str) or not _TIER_ID_RE.fullmatch(tier_id):
        raise TierValidationError("id must contain lowercase letters, digits, and hyphens")
    if not isinstance(name, str):
        raise TierValidationError("name must be a string")
    display_name = name.strip()
    normalized_tier_name(name)
    return {
        "id": tier_id,
        "name": display_name,
        "price_per_year": _canonical_price(price_per_year),
        "chance": _canonical_chance(chance),
    }


def _catalog_records(catalog: Any) -> list[dict[str, Any]]:
    if isinstance(catalog, Mapping):
        records = []
        for key, value in catalog.items():
            if not isinstance(value, Mapping):
                raise TierValidationError("each tier must be an object")
            record = dict(value)
            record.setdefault("id", key)
            records.append(record)
        return records
    if isinstance(catalog, (list, tuple)):
        return list(catalog)
    raise TierValidationError("tier catalog must be a mapping or list")


def validate_catalog(catalog: Any) -> dict[str, dict[str, Any]]:
    """Validate a complete catalog and return an insertion-ordered mapping."""
    result: dict[str, dict[str, Any]] = {}
    names: dict[str, str] = {}
    for raw in _catalog_records(catalog):
        record = validate_tier(raw)
        if record["id"] in result:
            raise TierValidationError(f"duplicate tier ID: {record['id']}")
        name_key = normalized_tier_name(record["name"])
        if name_key in names:
            raise DuplicateTierNameError(
                f"tier name duplicates {names[name_key]} after whitespace/case normalization"
            )
        names[name_key] = record["id"]
        result[record["id"]] = record
    return result


def format_annual_price(price_per_year: Any) -> str:
    """Format a validated annual RON amount for the public plan list."""
    amount = Decimal(_canonical_price(price_per_year))
    display = format(amount, "f")
    return f"{display} lei / an"


def format_chance(chance: Any) -> str:
    """Format a probability as a percent without whole-percent rounding."""
    probability = _canonical_chance(chance)
    percent = Decimal(str(probability)) * 100
    display = format(percent, "f")
    if "." in display:
        display = display.rstrip("0").rstrip(".")
    return f"{display}%"


def encode_tier_catalog(catalog: Any) -> str:
    """Encode a catalog as versioned default overrides plus custom tiers."""
    catalog = validate_catalog(catalog)
    overrides = {}
    for tier_id, default in DEFAULT_SPONSOR_TIERS.items():
        if tier_id not in catalog:
            continue
        current = catalog[tier_id]
        changed = {
            field: current[field]
            for field in ("name", "price_per_year", "chance")
            if current[field] != default[field]
        }
        if changed:
            overrides[tier_id] = changed
    custom = [record for tier_id, record in catalog.items() if tier_id not in DEFAULT_SPONSOR_TIERS]
    return json.dumps(
        {"version": CATALOG_VERSION, "overrides": overrides, "custom": custom},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _decode_document(document: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(document, Mapping):
        raise TierCatalogError("sponsor tier catalog must be a JSON object")
    if document.get("version") != CATALOG_VERSION:
        raise TierCatalogError(
            f"unsupported sponsor tier catalog version: {document.get('version')!r}"
        )

    if "tiers" in document:
        if set(document) != {"version", "tiers"}:
            raise TierCatalogError("unsupported sponsor tier catalog fields")
        try:
            return validate_catalog(document["tiers"])
        except SponsorTierError as exc:
            raise TierCatalogError(str(exc)) from exc

    if set(document) - {"version", "overrides", "custom"}:
        raise TierCatalogError("unsupported sponsor tier catalog fields")
    overrides = document.get("overrides", {})
    custom = document.get("custom", [])
    if not isinstance(overrides, Mapping) or not isinstance(custom, list):
        raise TierCatalogError("overrides must be an object and custom must be a list")

    result = deepcopy(DEFAULT_SPONSOR_TIERS)
    try:
        for tier_id, raw in overrides.items():
            if tier_id not in result:
                raise TierCatalogError(f"unknown default tier override: {tier_id}")
            if not isinstance(raw, Mapping):
                raise TierCatalogError(f"override for {tier_id} must be an object")
            if set(raw) - {"name", "price_per_year", "chance"}:
                raise TierCatalogError(f"unsupported fields in override for {tier_id}")
            record = validate_tier(
                tier_id,
                raw.get("name", result[tier_id]["name"]),
                raw.get("price_per_year", result[tier_id]["price_per_year"]),
                raw.get("chance", result[tier_id]["chance"]),
            )
            result[tier_id] = record
    except SponsorTierError as exc:
        raise TierCatalogError(str(exc)) from exc

    try:
        custom_catalog = validate_catalog(custom)
    except SponsorTierError as exc:
        raise TierCatalogError(str(exc)) from exc
    for tier_id, record in custom_catalog.items():
        if tier_id in result:
            raise TierCatalogError(f"custom tier reuses existing ID: {tier_id}")
        if not _CUSTOM_TIER_ID_RE.fullmatch(tier_id):
            raise TierCatalogError(f"custom tier ID is invalid: {tier_id}")
        result[tier_id] = record
    try:
        return validate_catalog(result)
    except SponsorTierError as exc:
        raise TierCatalogError(str(exc)) from exc


def decode_tier_catalog(value: Any) -> dict[str, dict[str, Any]]:
    """Decode a versioned JSON catalog into a validated tier mapping."""
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            document = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise TierCatalogError("sponsor tier catalog is not valid JSON") from exc
    else:
        document = value
    return _decode_document(document)
