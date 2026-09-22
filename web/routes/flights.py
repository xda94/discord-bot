"""Flight credential and tracker administration routes."""

import logging
from datetime import date, datetime

from flask import Blueprint, jsonify, request

from db import (
    add_flight_tracker, delete_flight_api_credentials, delete_flight_tracker,
    get_flight_api_credentials, get_flight_price_history, get_flight_tracker,
    get_user_flight_trackers, set_flight_api_credentials,
)
from flight_provider import (
    SUPPORTED_CURRENCIES, FlightProviderError, SerpApiFlightProvider,
    normalize_iata, parse_iso_date,
)
from web.auth import require_token
from web.helpers import discord_id as _discord_id, is_int as _is_int

logger = logging.getLogger("flask_api")
blueprint = Blueprint("flights", __name__)


def _validate_flight_tracker_payload(data):
    required = ("user_id", "origin", "destination", "start_date", "end_date")
    if not data or any(name not in data for name in required):
        raise ValueError("Missing user_id, origin, destination, start_date, or end_date")
    user_id = _discord_id(data["user_id"], "user_id")
    origin = normalize_iata(data["origin"])
    destination = normalize_iata(data["destination"])
    if origin == destination:
        raise ValueError("Origin and destination must be different")
    start = parse_iso_date(data["start_date"])
    end = parse_iso_date(data["end_date"])
    if start < date.today():
        raise ValueError("The start date cannot be in the past")
    if end <= start:
        raise ValueError("The end date must be after the start date")
    adults = data.get("adults", 1)
    if not _is_int(adults) or not 1 <= adults <= 9:
        raise ValueError("adults must be an integer between 1 and 9")
    currency = str(data.get("currency", "EUR")).upper()
    if currency not in SUPPORTED_CURRENCIES:
        raise ValueError(
            f"currency must be one of: {', '.join(SUPPORTED_CURRENCIES)}"
        )
    return {
        "user_id": user_id,
        "origin": origin,
        "destination": destination,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "adults": adults,
        "currency": currency,
    }

@blueprint.route("/flights/credentials", methods=["GET"])
@require_token
def api_get_flight_credentials_status():
    user_id = request.args.get("user_id", type=int)
    if user_id is None:
        return jsonify({"error": "Missing user_id query parameter"}), 400
    credentials = get_flight_api_credentials(user_id)
    return jsonify(
        {
            "user_id": user_id,
            "configured": credentials is not None,
            "updated_at": credentials["updated_at"] if credentials else None,
        }
    )


@blueprint.route("/flights/credentials", methods=["POST"])
@require_token
def api_set_flight_credentials():
    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({"error": "user_id is required"}), 400
    try:
        user_id = _discord_id(data.get("user_id"), "user_id")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    api_key = data.get("api_key")
    if not isinstance(api_key, str) or not 1 <= len(api_key.strip()) <= 200:
        return jsonify({"error": "api_key must be a non-empty string up to 200 characters"}), 400
    try:
        SerpApiFlightProvider(api_key=api_key.strip()).validate_credentials()
    except FlightProviderError as exc:
        return jsonify({"error": "Credential validation failed", "detail": str(exc)}), 400
    if not set_flight_api_credentials(user_id, api_key.strip()):
        return jsonify({"error": "Could not store credentials"}), 500
    logger.info("Validated and stored flight credentials through API for user %s", user_id)
    return jsonify({"status": "stored", "user_id": user_id}), 201


@blueprint.route("/flights/credentials", methods=["DELETE"])
@require_token
def api_delete_flight_credentials():
    data = request.get_json()
    user_id = data.get("user_id") if isinstance(data, dict) else None
    try:
        user_id = _discord_id(user_id, "user_id")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not delete_flight_api_credentials(user_id):
        return jsonify({"error": "Credentials not found"}), 404
    return jsonify({"status": "deleted", "user_id": user_id})


@blueprint.route("/flights/trackers", methods=["GET"])
@require_token
def api_get_flight_trackers():
    user_id = request.args.get("user_id", type=int)
    if user_id is None:
        return jsonify({"error": "Missing user_id query parameter"}), 400
    return jsonify({"user_id": user_id, "trackers": get_user_flight_trackers(user_id)})


@blueprint.route("/flights/trackers", methods=["POST"])
@require_token
def api_add_flight_tracker():
    try:
        values = _validate_flight_tracker_payload(request.get_json())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if get_flight_api_credentials(values["user_id"]) is None:
        return jsonify({"error": "No validated SerpApi credentials are configured for this user"}), 409
    tracker_id = add_flight_tracker(
        values["user_id"],
        values["origin"],
        values["destination"],
        values["start_date"],
        values["end_date"],
        adults=values["adults"],
        currency=values["currency"],
    )
    if not tracker_id:
        return jsonify({"error": "That exact tracker already exists"}), 409
    # Unlike the Discord command, this data/config route intentionally does
    # not make an immediate paid provider request. The normal bot cadence will
    # pick it up using the user's stored credentials.
    return jsonify(get_flight_tracker(tracker_id, values["user_id"])), 201


@blueprint.route("/flights/trackers/<int:tracker_id>", methods=["GET"])
@require_token
def api_get_flight_tracker(tracker_id):
    user_id = request.args.get("user_id", type=int)
    if user_id is None:
        return jsonify({"error": "Missing user_id query parameter"}), 400
    tracker = get_flight_tracker(tracker_id, user_id)
    if tracker is None:
        return jsonify({"error": "Tracker not found"}), 404
    return jsonify(tracker)


@blueprint.route("/flights/trackers/<int:tracker_id>", methods=["DELETE"])
@require_token
def api_delete_flight_tracker(tracker_id):
    data = request.get_json()
    user_id = data.get("user_id") if isinstance(data, dict) else None
    try:
        user_id = _discord_id(user_id, "user_id")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not delete_flight_tracker(user_id, tracker_id):
        return jsonify({"error": "Tracker not found"}), 404
    return jsonify({"status": "deleted", "id": tracker_id})


@blueprint.route("/flights/trackers/<int:tracker_id>/history", methods=["GET"])
@require_token
def api_get_flight_tracker_history(tracker_id):
    user_id = request.args.get("user_id", type=int)
    if user_id is None:
        return jsonify({"error": "Missing user_id query parameter"}), 400
    if get_flight_tracker(tracker_id, user_id) is None:
        return jsonify({"error": "Tracker not found"}), 404
    history = get_flight_price_history(tracker_id, user_id)
    return jsonify(
        {
            "tracker_id": tracker_id,
            "history": [
                {
                    "price": price,
                    "currency": currency,
                    "departure_date": departure,
                    "return_date": returning,
                    "checked_at": checked_at,
                }
                for price, currency, departure, returning, checked_at in history
            ],
        }
    )


# --- Settings Routes ---
