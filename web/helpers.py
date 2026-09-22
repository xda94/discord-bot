"""Request and response helpers shared across route groups."""

from __future__ import annotations

import json

from flask import request

DISCORD_ID_FIELDS = {
    "guild_id",
    "user_id",
    "channel_id",
    "requester_user_id",
    "message_id",
    "scope_id",
}


def is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def discord_id(value, field_name):
    parsed = None
    if is_int(value) and value >= 0:
        parsed = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        parsed = int(value)
    if parsed is not None and parsed <= 9_223_372_036_854_775_807:
        return parsed
    raise ValueError(f"{field_name} must be a non-negative integer or decimal string")


def stringify_discord_ids(value, key=None):
    if isinstance(value, dict):
        return {name: stringify_discord_ids(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [stringify_discord_ids(item, key) for item in value]
    if key in DISCORD_ID_FIELDS and value is not None:
        return str(value)
    return value


def format_discord_ids(response):
    if (
        request.headers.get("X-Discord-ID-Format", "").lower() == "string"
        and response.is_json
    ):
        payload = response.get_json(silent=True)
        if payload is not None:
            response.set_data(
                json.dumps(stringify_discord_ids(payload), separators=(",", ":"))
            )
            response.headers["Content-Type"] = "application/json"
            response.headers["Content-Length"] = str(len(response.get_data()))
    return response
