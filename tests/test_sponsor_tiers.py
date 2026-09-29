import json
import math

import pytest

import db
from sponsor_tiers import (
    DEFAULT_SPONSOR_TIERS,
    DuplicateTierNameError,
    TierCatalogError,
    TierValidationError,
    decode_tier_catalog,
    encode_tier_catalog,
    format_annual_price,
    format_chance,
    validate_tier,
)


def test_defaults_and_formatting_are_stable():
    assert list(DEFAULT_SPONSOR_TIERS) == ["standard", "entuziast", "premium", "ultra"]
    assert format_annual_price("6") == "6 lei / an"
    assert format_annual_price("12.50") == "12.50 lei / an"
    assert format_chance(0.125) == "12.5%"
    assert format_chance(1) == "100%"


def test_catalog_round_trip_preserves_decimal_price_and_fractional_chance():
    catalog = {
        "standard": {
            "id": "standard",
            "name": "  A Standard  ",
            "price_per_year": "12.50",
            "chance": 0.125,
        }
    }
    decoded = decode_tier_catalog(encode_tier_catalog(catalog))
    assert decoded["standard"] == {
        "id": "standard",
        "name": "A Standard",
        "price_per_year": "12.50",
        "chance": 0.125,
    }


@pytest.mark.parametrize(
    "record",
    [
        {"id": "x", "name": "", "price_per_year": "1", "chance": 0},
        {"id": "x", "name": "x" * 81, "price_per_year": "1", "chance": 0},
        {"id": "x", "name": "x\n", "price_per_year": "1", "chance": 0},
        {"id": "x", "name": "x", "price_per_year": "-1", "chance": 0},
        {"id": "x", "name": "x", "price_per_year": "1.001", "chance": 0},
        {"id": "x", "name": "x", "price_per_year": "1", "chance": -0.01},
        {"id": "x", "name": "x", "price_per_year": "1", "chance": 1.01},
        {"id": "x", "name": "x", "price_per_year": True, "chance": 0},
        {"id": "x", "name": "x", "price_per_year": "1", "chance": True},
        {"id": "x", "name": "x", "price_per_year": "1", "chance": math.inf},
    ],
)
def test_invalid_tiers_are_rejected(record):
    with pytest.raises(TierValidationError):
        validate_tier(record)


def test_duplicate_names_use_case_and_whitespace_normalization():
    with pytest.raises(DuplicateTierNameError):
        encode_tier_catalog(
            [
                {"id": "one", "name": "Tier One", "price_per_year": "1", "chance": 0},
                {"id": "two", "name": " tier   one ", "price_per_year": "2", "chance": 1},
            ]
        )


def test_unsupported_catalog_versions_are_not_silently_replaced():
    with pytest.raises(TierCatalogError):
        decode_tier_catalog(json.dumps({"version": 99, "tiers": []}))


def test_sponsor_catalog_persistence_overlays_defaults_and_preserves_active_state(tmp_db):
    assert db.get_sponsor_tiers()["premium"]["price_per_year"] == "10"
    assert db.get_setting("sponsor_tiers") is None
    db.set_setting("sponsor", "Alice")
    db.set_setting("sponsor_tier", "ultra")
    saved = db.save_sponsor_tier("standard", "Custom Standard", "12.50", 0.125)
    created = db.save_sponsor_tier(None, "One-off", "0", 1)

    assert saved["id"] == "standard"
    assert created["id"].startswith("tier-")
    assert db.get_sponsor_tiers()["standard"]["chance"] == 0.125
    assert db.get_sponsor_tiers()[created["id"]]["price_per_year"] == "0"
    assert db.get_setting("sponsor") == "Alice"
    assert db.get_setting("sponsor_tier") == "ultra"


def test_corrupt_persisted_catalog_fails_reads_and_writes(tmp_db):
    db.set_setting("sponsor_tiers", '{"version": 99}')
    with pytest.raises(TierCatalogError):
        db.get_sponsor_tiers()
    with pytest.raises(TierCatalogError):
        db.save_sponsor_tier(None, "Tier", "1", 0)
