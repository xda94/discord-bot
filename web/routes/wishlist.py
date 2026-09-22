"""Wishlist administration routes."""

import logging

from flask import Blueprint, jsonify, request

from db import (
    add_price_history, add_scraped_item, delete_scraped_item,
    get_all_scraped_items, get_price_history, get_scraped_item,
    set_scraped_item_restock_only, set_scraped_item_target,
    update_scraped_item_check_status, update_scraped_item_status,
)
from web.auth import require_token
from web.helpers import discord_id as _discord_id
from wishlist.refresh import refresh_item
from wishlist.scraper import (
    FAILURE_BLOCKED, FAILURE_UNSUPPORTED, PriceScraper, _domain,
    _is_valid_http_url,
)
from flight_provider import SUPPORTED_CURRENCIES

logger = logging.getLogger("flask_api")
blueprint = Blueprint("wishlist", __name__)

_price_scraper = PriceScraper()


def _serialize_scraped_item(item):
    if item is None:
        return None
    return {
        "id": item[0],
        "user_id": item[1],
        "url": item[2],
        "last_price": item[3],
        "in_stock": bool(item[4]) if item[4] is not None else None,
        "title": item[5],
        "currency": item[6],
        "last_alert_kind": item[7],
        "last_alert_price": item[8],
        "target_price": item[9],
        "target_currency": item[10],
        "target_alerted": bool(item[11]),
        "restock_only": bool(item[12]),
        "last_checked_at": item[13],
        "last_check_status": item[14],
    }


@blueprint.route("/wishlist/add", methods=["POST"])
@require_token
def api_add_scrape():
    """Add a URL to a user's tracking list, *only if* it can actually be
    scraped right now.

    The previous version of this endpoint inserted a row with no title /
    price / currency / stock and relied on the bot's 12-hour loop to
    populate it later. That created "dead" rows for unscrapeable URLs
    (typos, JS-rendered SPAs, perma-blocked sites) that the user then
    had to discover and remove manually. We now run the scrape inline
    and reject the request if it fails, so the DB only ever holds rows
    we've successfully extracted at least one signal from.

    Note: `_price_scraper.fetch()` can take up to ~15s (HTTP timeout).
    That's acceptable here because `/wishlist/add` is an admin endpoint,
    not a hot path — and a long-but-honest response is much better than
    a fast success that becomes a silent failure 12 hours later.
    """
    data = request.get_json()
    if not data or "user_id" not in data or "url" not in data:
        logger.warning(f"BadRequest: Missing fields in /wishlist/add. IP: {request.remote_addr}")
        return jsonify({"error": "Missing user_id or url"}), 400

    try:
        user_id = _discord_id(data["user_id"], "user_id")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    url = data["url"]

    if not _is_valid_http_url(url):
        logger.warning(f"BadRequest: Invalid URL in /wishlist/add: {url!r}")
        return jsonify({"error": "Invalid URL"}), 400

    result = _price_scraper.fetch(url)

    if result.failure == FAILURE_BLOCKED:
        logger.warning(f"/wishlist/add rejected (blocked) for {url}")
        return jsonify({
            "error": "blocked",
            "detail": (
                "The target domain blocked the scraper (timeout or anti-bot "
                "protection). The link was not added."
            ),
        }), 502
    if result.failure == FAILURE_UNSUPPORTED and not result.has_data:
        logger.warning(f"/wishlist/add rejected (unsupported) for {url}")
        return jsonify({
            "error": "unsupported",
            "detail": (
                "Reached the page but couldn't extract any price/stock data "
                "in the supported formats (JSON-LD, meta tags, plain text). "
                "Most likely a JavaScript-rendered SPA. The link was not added."
            ),
        }), 422

    item_id = add_scraped_item(
        user_id, url,
        title=result.title, price=result.price,
        stock=result.in_stock, currency=result.currency,
    )
    if not item_id:
        return jsonify({"error": "Already tracked"}), 409

    if result.price is not None:
        add_price_history(item_id, result.price)

    logger.info(
        f"Scrape item added via API for User {user_id}: {url} "
        f"(price={result.price} {result.currency}, in_stock={result.in_stock})"
    )
    return jsonify({
        "status": "ok",
        "id": item_id,
        "title": result.title,
        "price": result.price,
        "currency": result.currency,
        "in_stock": result.in_stock,
    }), 201

@blueprint.route("/wishlist/remove", methods=["DELETE"])
@require_token
def api_remove_scrape():
    data = request.get_json()
    if not data or "user_id" not in data or "url" not in data:
        return jsonify({"error": "Missing user_id or url"}), 400

    try:
        user_id = _discord_id(data["user_id"], "user_id")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    success = delete_scraped_item(user_id, data["url"])
    if success:
        logger.info(f"Scrape item removed via API for User {user_id}")
        return jsonify({"status": "removed"})
    else:
        return jsonify({"error": "Item not found"}), 404

@blueprint.route("/wishlist/all", methods=["GET"])
@require_token
def api_get_all_scrapes():
    try:
        items = get_all_scraped_items()
        # `get_all_scraped_items` returns a 15-tuple:
        #   (id, user_id, url, last_price, last_stock_status, title,
        #    currency, last_alert_kind, last_alert_price, target_price,
        #    target_currency, target_alerted, restock_only, last_checked_at,
        #    last_check_status)
        # `len(i) > N` guards are belt-and-braces in case an older
        # schema (pre-alerts migration) is queried before init_db has
        # had a chance to ALTER the table.
        result = [
            {
                "id": i[0],
                "user_id": i[1],
                "url": i[2],
                "last_price": i[3],
                "in_stock": bool(i[4]) if i[4] is not None else None,
                "title": i[5] if len(i) > 5 else None,
                "currency": i[6] if len(i) > 6 else None,
                "last_alert_kind": i[7] if len(i) > 7 else None,
                "last_alert_price": i[8] if len(i) > 8 else None,
                "target_price": i[9] if len(i) > 9 else None,
                "target_currency": i[10] if len(i) > 10 else None,
                "target_alerted": bool(i[11]) if len(i) > 11 else False,
                "restock_only": bool(i[12]) if len(i) > 12 else False,
                "last_checked_at": i[13] if len(i) > 13 else None,
                "last_check_status": i[14] if len(i) > 14 else None,
            } for i in items
        ]
        return jsonify(result)
    except Exception:
        logger.exception("Error in /wishlist/all")
        return jsonify({"error": "Internal server error"}), 500


@blueprint.route("/wishlist/preferences", methods=["GET"])
@require_token
def api_get_wishlist_preferences():
    user_id = request.args.get("user_id", type=int)
    url = request.args.get("url", type=str)
    if user_id is None or not url:
        return jsonify({"error": "Missing user_id or url query parameter"}), 400
    item = get_scraped_item(user_id, url)
    if item is None:
        return jsonify({"error": "Item not found"}), 404
    return jsonify(_serialize_scraped_item(item))


@blueprint.route("/wishlist/history", methods=["GET"])
@require_token
def api_get_wishlist_history():
    user_id = request.args.get("user_id", type=int)
    url = request.args.get("url", type=str)
    if user_id is None or not url:
        return jsonify({"error": "Missing user_id or url query parameter"}), 400
    item = get_scraped_item(user_id, url)
    if item is None:
        return jsonify({"error": "Item not found"}), 404
    history = get_price_history(user_id, url)
    return jsonify(
        {
            "item": _serialize_scraped_item(item),
            "history": [
                {"price": price, "timestamp": timestamp}
                for price, timestamp, _title in history
            ],
        }
    )


@blueprint.route("/wishlist/preferences", methods=["PUT"])
@require_token
def api_set_wishlist_preferences():
    data = request.get_json()
    if not isinstance(data, dict) or not isinstance(data.get("url"), str):
        return jsonify({"error": "user_id and url are required"}), 400
    try:
        user_id = _discord_id(data.get("user_id"), "user_id")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    url = data["url"]
    if get_scraped_item(user_id, url) is None:
        return jsonify({"error": "Item not found"}), 404

    changed = False
    if data.get("clear_target") is True:
        if not set_scraped_item_target(user_id, url, None, None):
            return jsonify({"error": "Could not clear target"}), 500
        changed = True
    elif "target_price" in data or "target_currency" in data:
        price = data.get("target_price")
        currency = data.get("target_currency")
        if (
            not isinstance(price, (int, float))
            or isinstance(price, bool)
            or price <= 0
            or not isinstance(currency, str)
            or currency.upper() not in SUPPORTED_CURRENCIES
        ):
            return jsonify(
                {
                    "error": "target_price must be positive and target_currency "
                    f"must be one of {', '.join(SUPPORTED_CURRENCIES)}",
                }
            ), 400
        if not set_scraped_item_target(user_id, url, float(price), currency):
            return jsonify({"error": "Could not set target"}), 500
        changed = True

    if "restock_only" in data:
        if not isinstance(data["restock_only"], bool):
            return jsonify({"error": "restock_only must be a boolean"}), 400
        if not set_scraped_item_restock_only(user_id, url, data["restock_only"]):
            return jsonify({"error": "Could not set restock_only"}), 500
        changed = True

    if not changed:
        return jsonify({"error": "Provide target_price/target_currency, clear_target, or restock_only"}), 400
    return jsonify(_serialize_scraped_item(get_scraped_item(user_id, url)))


@blueprint.route("/wishlist/refresh", methods=["POST"])
@require_token
def api_refresh_wishlist_item():
    """Refresh data only; this route never sends a Discord DM or LLM request."""
    data = request.get_json()
    if not isinstance(data, dict) or not isinstance(data.get("url"), str):
        return jsonify({"error": "user_id and url are required"}), 400
    try:
        user_id = _discord_id(data.get("user_id"), "user_id")
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    item = get_scraped_item(user_id, data["url"])
    if item is None:
        return jsonify({"error": "Item not found"}), 404

    result = refresh_item(item, _price_scraper)
    status = result.failure or "ok"
    if result.failure == FAILURE_BLOCKED:
        return jsonify(
            {
                "error": "blocked",
                "source": _domain(item[2]),
                "last_check_status": status,
            }
        ), 502
    if result.failure == FAILURE_UNSUPPORTED and not result.has_data:
        return jsonify(
            {
                "error": "unsupported",
                "source": _domain(item[2]),
                "last_check_status": status,
            }
        ), 422

    refreshed = _serialize_scraped_item(get_scraped_item(user_id, data["url"]))
    refreshed["source"] = _domain(item[2])
    return jsonify(refreshed)


# --- Flight tracker configuration ------------------------------------------
