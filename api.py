import hmac
import logging
import os
import sys
import threading
import time
from datetime import date, datetime
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from db import (
    add_joke,
    add_price_history,
    add_reminder,
    add_response,
    add_scraped_item,
    clear_guild_joke_config,
    delete_flight_api_credentials,
    delete_flight_tracker,
    delete_joke,
    delete_reminder,
    delete_scraped_item,
    get_all_guild_joke_configs,
    get_all_jokes,
    get_all_reminders,
    get_all_responses,
    get_all_scraped_items,
    get_flight_price_history,
    get_flight_api_credentials,
    get_flight_tracker,
    get_guild_joke_config,
    get_joke_by_id,
    get_llm_feedback_summary,
    get_scraped_item,
    get_top_keywords,
    get_top_keywords_by_user,
    get_user_flight_trackers,
    init_db,
    is_guild_inactivity_enabled,
    remove_response,
    reset_all_guild_joke_sent,
    set_flight_api_credentials,
    set_guild_inactivity_enabled,
    set_guild_joke_config,
    set_scraped_item_restock_only,
    set_scraped_item_target,
    update_joke,
    update_scraped_item_check_status,
    update_scraped_item_status,
    get_setting,
    set_setting,
    add_flight_tracker,
)
from flight_provider import (
    SUPPORTED_CURRENCIES,
    FlightProviderError,
    SerpApiFlightProvider,
    normalize_iata,
    parse_iso_date,
)
from llm_client import LlamaCppError, get_allowed_models, get_mention_model
from logger import setup_logger
from scraper import (
    FAILURE_BLOCKED,
    FAILURE_UNSUPPORTED,
    PriceScraper,
    _domain,
    _is_valid_http_url,
)

logger = setup_logger("flask_api", "api.log")

load_dotenv()

HOST = os.getenv("HOST")
PORT = os.getenv("PORT")
API_TOKEN = os.getenv("API_TOKEN")

# Fail loud and early on missing/invalid required config. Without this you get
# a `TypeError: int() argument must be a string` (PORT) or an opaque
# `socket.gaierror` (HOST) from `app.run()` instead of a clear actionable
# message in `api.log`.
_missing = [name for name, val in (("HOST", HOST), ("PORT", PORT)) if not val]
if _missing:
    logger.critical(
        f"Required env var(s) missing: {', '.join(_missing)}. "
        f"Set them in your .env file and restart. Refusing to start."
    )
    sys.exit(1)
try:
    int(PORT)
except (TypeError, ValueError):
    logger.critical(f"PORT is not a valid integer (got {PORT!r}). Refusing to start.")
    sys.exit(1)

app = Flask(__name__)

# Reduce Flask's default verbose logging
log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)

# --- Authentication ---------------------------------------------------------
#
# When `API_TOKEN` is set, every route requires `Authorization: Bearer <token>`
# matching it. Token comparison uses `hmac.compare_digest` to dodge timing
# attacks. When the env var is unset the API runs OPEN — backward-compatible
# with existing deployments — but logs a critical warning at startup so the
# state is unambiguous in `api.log`.

if not API_TOKEN:
    logger.critical(
        "API_TOKEN is not set — the Flask API is running UNAUTHENTICATED. "
        "Set API_TOKEN in .env to require a bearer token on every request."
    )


def require_token(f):
    """Reject any request whose Authorization header doesn't carry the
    configured bearer token. No-op when `API_TOKEN` is unset."""

    @wraps(f)
    def wrapper(*args, **kwargs):
        if not API_TOKEN:
            return f(*args, **kwargs)
        auth = request.headers.get("Authorization", "")
        prefix = "Bearer "
        if not auth.startswith(prefix) or not hmac.compare_digest(
            auth[len(prefix):], API_TOKEN
        ):
            logger.warning(
                f"Unauthorized request to {request.path} from {request.remote_addr}"
            )
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)

    return wrapper


# --- Lazy DB init -----------------------------------------------------------
#
# Initialise the DB on the first request rather than at import time. This:
#   - Keeps `import api` (e.g. in tests) cheap and side-effect-free.
#   - Still works when the app is hosted under a WSGI server like gunicorn
#     (where the `if __name__ == "__main__"` block never runs).
#   - Is safe under `threaded=True`: double-checked locking with an
#     idempotent `init_db()` (it's all `CREATE TABLE IF NOT EXISTS`).

_db_initialized = False
_db_init_lock = threading.Lock()


@app.before_request
def _ensure_db_initialized():
    global _db_initialized
    if _db_initialized:
        return
    with _db_init_lock:
        if not _db_initialized:
            init_db()
            _db_initialized = True


def _is_int(value) -> bool:
    """JSON booleans are ints in Python, but never valid Discord IDs."""
    return isinstance(value, int) and not isinstance(value, bool)


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


def _validate_flight_tracker_payload(data):
    """Pure validation shared by the HTTP flight configuration routes."""
    required = ("user_id", "origin", "destination", "start_date", "end_date")
    if not data or any(name not in data for name in required):
        raise ValueError("Missing user_id, origin, destination, start_date, or end_date")
    if not _is_int(data["user_id"]):
        raise ValueError("user_id must be an integer")

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
        "user_id": data["user_id"],
        "origin": origin,
        "destination": destination,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "adults": adults,
        "currency": currency,
    }


# --- Keywords Routes ---

@app.route("/keywords/add", methods=["POST"])
@require_token
def api_keywords_add():
    data = request.get_json()
    if not data or "keyword" not in data or "response" not in data or "guild_id" not in data:
        logger.warning(
            f"BadRequest: Missing fields in POST /keywords/add. IP: {request.remote_addr}"
        )
        return jsonify({"error": "Missing keyword, response, or guild_id"}), 400

    guild_id = data["guild_id"]
    if not isinstance(guild_id, int):
        return jsonify({"error": "guild_id must be an integer"}), 400

    add_response(data["keyword"], data["response"], guild_id)
    logger.info(
        f"Keyword added via API: '{data['keyword']}' for guild {guild_id} "
        f"from {request.remote_addr}"
    )
    return jsonify({"status": "ok"})


@app.route("/keywords/delete", methods=["DELETE"])
@require_token
def api_keywords_delete():
    data = request.get_json()
    if not data or "keyword" not in data or "guild_id" not in data:
        return jsonify({"error": "Missing keyword or guild_id"}), 400

    guild_id = data["guild_id"]
    if not isinstance(guild_id, int):
        return jsonify({"error": "guild_id must be an integer"}), 400

    keyword = data["keyword"]
    response = data.get("response")

    success = remove_response(keyword, guild_id, response)
    if success:
        logger.info(f"Keyword removed via API: '{keyword}' in guild {guild_id}")
        return jsonify({"status": "deleted"})
    else:
        logger.warning(
            f"DELETE /keywords/delete: '{keyword}' not found in guild {guild_id}."
        )
        return jsonify({"error": "Keyword or response not found"}), 404


@app.route("/keywords/get", methods=["GET"])
@require_token
def api_keywords_get():
    guild_id = request.args.get("guild_id", type=int)
    if guild_id is None:
        return jsonify({"error": "Missing guild_id query parameter"}), 400
    logger.info(
        f"Fetching keywords for guild {guild_id}. Requested by {request.remote_addr}"
    )
    responses = get_all_responses(guild_id)
    return jsonify(responses)


@app.route("/keywords/top", methods=["GET"])
@require_token
def api_keywords_top():
    guild_id = request.args.get("guild_id", type=int)
    user_id = request.args.get("user_id", type=int)
    limit = request.args.get("limit", default=10, type=int)
    if guild_id is None:
        return jsonify({"error": "Missing guild_id query parameter"}), 400
    if limit is None or not 1 <= limit <= 100:
        return jsonify({"error": "limit must be an integer between 1 and 100"}), 400
    rows = (
        get_top_keywords_by_user(guild_id, user_id, limit)
        if user_id is not None
        else get_top_keywords(guild_id, limit)
    )
    return jsonify(
        {
            "guild_id": guild_id,
            "user_id": user_id,
            "keywords": [{"keyword": keyword, "count": count} for keyword, count in rows],
        }
    )


# --- Reminder Routes ---

@app.route("/reminders/add", methods=["POST"])
@require_token
def api_add_reminder():
    data = request.get_json()
    required = ["user_id", "channel_id", "remind_at", "message"]
    if not data or not all(k in data for k in required):
        logger.warning("Invalid payload for /reminders/add")
        return jsonify({"error": "Missing required fields"}), 400

    add_reminder(
        data["user_id"], 
        data["channel_id"], 
        data["remind_at"], 
        data["message"]
    )
    logger.info(f"Reminder set via API for User ID {data['user_id']}")
    return jsonify({"status": "reminder_set"})

@app.route("/reminders/delete/<int:reminder_id>", methods=["DELETE"])
@require_token
def api_delete_reminder(reminder_id):
    delete_reminder(reminder_id)
    logger.info(f"Reminder {reminder_id} deleted via API")
    return jsonify({"status": "deleted", "id": reminder_id})

@app.route("/reminders/all", methods=["GET"]) 
@require_token
def api_get_all_reminders(): 
    try: 
        reminders = get_all_reminders() 
        # Mapping the database rows to a clean JSON format
        result = [ 
            {
                "id": r[0], 
                "user_id": r[1], 
                "channel_id": r[2], 
                "message": r[3], 
                "remind_at": r[4]
            } 
            for r in reminders 
        ] 
        logger.info(f"All reminders fetched. Count: {len(result)}")
        return jsonify(result) 
    except Exception: 
        logger.exception("Error in /reminders/all") 
        return jsonify({"error": "Internal server error"}), 500

# --- Joke Routes ---

@app.route("/jokes", methods=["GET"])
@require_token
def api_get_all_jokes():
    try:
        jokes = get_all_jokes()
        result = [{"id": j[0], "text": j[1], "sent": bool(j[2])} for j in jokes]
        logger.info(f"All jokes fetched. Count: {len(result)}")
        return jsonify(result)
    except Exception:
        logger.exception("Error in GET /jokes")
        return jsonify({"error": "Internal server error"}), 500

@app.route("/jokes/<int:joke_id>", methods=["GET"])
@require_token
def api_get_joke(joke_id):
    joke = get_joke_by_id(joke_id)
    if joke is None:
        return jsonify({"error": "Joke not found"}), 404
    return jsonify({"id": joke[0], "text": joke[1], "sent": bool(joke[2])})

@app.route("/jokes", methods=["POST"])
@require_token
def api_add_joke():
    data = request.get_json()
    if not data or "text" not in data:
        logger.warning(f"BadRequest: Missing 'text' in POST /jokes. IP: {request.remote_addr}")
        return jsonify({"error": "Missing 'text' field"}), 400

    add_joke(data["text"])
    logger.info(f"Joke added via API from {request.remote_addr}")
    return jsonify({"status": "ok"}), 201

@app.route("/jokes/<int:joke_id>", methods=["PUT"])
@require_token
def api_update_joke(joke_id):
    data = request.get_json()
    if not data or "text" not in data:
        return jsonify({"error": "Missing 'text' field"}), 400

    success = update_joke(joke_id, data["text"])
    if success:
        logger.info(f"Joke {joke_id} updated via API")
        return jsonify({"status": "updated", "id": joke_id})
    else:
        return jsonify({"error": "Joke not found"}), 404

@app.route("/jokes/<int:joke_id>", methods=["DELETE"])
@require_token
def api_delete_joke(joke_id):
    success = delete_joke(joke_id)
    if success:
        logger.info(f"Joke {joke_id} deleted via API")
        return jsonify({"status": "deleted", "id": joke_id})
    else:
        return jsonify({"error": "Joke not found"}), 404

@app.route("/jokes/reset", methods=["POST"])
@require_token
def api_reset_jokes():
    """Wipe per-guild sent-joke history for every guild so all pools
    recycle simultaneously. The joke pool itself (the `jokes` table) is
    untouched — this only clears the "which jokes has each guild
    already received" tracking."""
    reset_all_guild_joke_sent()
    logger.info("All guild joke histories reset via API")
    return jsonify({"status": "reset"})


# --- Per-Guild Joke Schedule Routes ---
#
# `GET /jokes/guilds` — list every guild that has /joke-activation set
# `GET /jokes/guilds/<guild_id>` — one guild's config, or 404
# `PUT /jokes/guilds/<guild_id>` — activate / update (body: channel_id, send_time)
# `DELETE /jokes/guilds/<guild_id>` — deactivate that guild

def _serialize_guild_joke_config(cfg):
    """Public-facing shape for the per-guild config dicts the DB
    returns (kept as a tiny helper so GET-one and GET-all stay
    consistent if we ever add fields)."""
    return {
        "guild_id": cfg["guild_id"],
        "channel_id": cfg["channel_id"],
        "send_time": cfg["send_time"],
        "last_sent_date": cfg["last_sent_date"],
    }


@app.route("/jokes/guilds", methods=["GET"])
@require_token
def api_get_all_guild_joke_configs():
    try:
        configs = get_all_guild_joke_configs()
        result = [_serialize_guild_joke_config(c) for c in configs]
        logger.info(f"Per-guild joke configs fetched. Count: {len(result)}")
        return jsonify(result)
    except Exception:
        logger.exception("Error in GET /jokes/guilds")
        return jsonify({"error": "Internal server error"}), 500


@app.route("/jokes/guilds/<int:guild_id>", methods=["GET"])
@require_token
def api_get_guild_joke_config(guild_id):
    cfg = get_guild_joke_config(guild_id)
    if cfg is None:
        return jsonify({"error": "Guild not configured"}), 404
    return jsonify(_serialize_guild_joke_config(cfg))


@app.route("/jokes/guilds/<int:guild_id>", methods=["PUT"])
@require_token
def api_put_guild_joke_config(guild_id):
    """Create or update a guild's daily-joke schedule.

    Body: `{ "channel_id": int, "send_time": "HH:MM" }`.

    Both fields are required because the upsert in the DB sets both
    unconditionally — a partial PUT would silently drop the omitted
    field on update. Use a fresh PUT to change either."""
    data = request.get_json()
    if not data or "channel_id" not in data or "send_time" not in data:
        logger.warning(
            f"BadRequest: PUT /jokes/guilds/{guild_id} missing channel_id "
            f"or send_time. IP: {request.remote_addr}"
        )
        return jsonify({"error": "Missing channel_id or send_time"}), 400

    channel_id = data["channel_id"]
    send_time = data["send_time"]

    if not isinstance(channel_id, int):
        return jsonify({"error": "channel_id must be an integer"}), 400

    try:
        datetime.strptime(send_time, "%H:%M")
    except (TypeError, ValueError):
        return jsonify({"error": "send_time must match HH:MM (e.g. 14:00)"}), 400

    set_guild_joke_config(guild_id, channel_id, send_time)
    logger.info(
        f"Guild {guild_id} joke config set via API: channel={channel_id} time={send_time}"
    )
    return jsonify({
        "status": "ok",
        "guild_id": guild_id,
        "channel_id": channel_id,
        "send_time": send_time,
    }), 200


@app.route("/jokes/guilds/<int:guild_id>", methods=["DELETE"])
@require_token
def api_delete_guild_joke_config(guild_id):
    removed = clear_guild_joke_config(guild_id)
    if not removed:
        return jsonify({"error": "Guild not configured"}), 404
    logger.info(f"Guild {guild_id} joke config cleared via API")
    return jsonify({"status": "deleted", "guild_id": guild_id})


# --- LLM and inactivity configuration --------------------------------------

@app.route("/llm/mention-model", methods=["GET"])
@require_token
def api_get_mention_model():
    try:
        allowed = get_allowed_models()
        stored = get_setting("mention_model")
        selected = stored if stored in allowed else get_mention_model()
        return jsonify({"model": selected, "allowed_models": list(allowed)})
    except LlamaCppError as exc:
        logger.error("Could not read mention-model configuration: %s", exc)
        return jsonify({"error": str(exc)}), 503


@app.route("/llm/mention-model", methods=["PUT"])
@require_token
def api_set_mention_model():
    data = request.get_json()
    model = data.get("model") if isinstance(data, dict) else None
    if not isinstance(model, str) or not model.strip():
        return jsonify({"error": "model must be a non-empty string"}), 400
    try:
        allowed = get_allowed_models()
    except LlamaCppError as exc:
        return jsonify({"error": str(exc)}), 503
    model = model.strip()
    if model not in allowed:
        return jsonify({"error": "Model is not allowed", "allowed_models": list(allowed)}), 400
    set_setting("mention_model", model)
    logger.info("Mention model changed through API")
    return jsonify({"status": "updated", "model": model})


@app.route("/llm/feedback/summary", methods=["GET"])
@require_token
def api_llm_feedback_summary():
    guild_id = request.args.get("guild_id", type=int)
    if guild_id is None:
        return jsonify({"error": "Missing guild_id query parameter"}), 400
    rows = get_llm_feedback_summary(guild_id)
    groups = []
    for category, model, prompt_version, total, positive, negative in rows:
        groups.append(
            {
                "category": category,
                "model": model,
                "prompt_version": prompt_version,
                "ratings": total,
                "up": positive,
                "down": negative,
                "approval_percent": round(positive / total * 100, 1) if total else 0,
                "ready_to_compare": total >= 10,
            }
        )
    return jsonify({"guild_id": guild_id, "groups": groups})


@app.route("/inactivity/guilds/<int:guild_id>", methods=["GET"])
@require_token
def api_get_guild_inactivity(guild_id):
    return jsonify({"guild_id": guild_id, "enabled": is_guild_inactivity_enabled(guild_id)})


@app.route("/inactivity/guilds/<int:guild_id>", methods=["PUT"])
@require_token
def api_set_guild_inactivity(guild_id):
    data = request.get_json()
    enabled = data.get("enabled") if isinstance(data, dict) else None
    if not isinstance(enabled, bool):
        return jsonify({"error": "enabled must be a boolean"}), 400
    set_guild_inactivity_enabled(guild_id, enabled)
    return jsonify({"status": "updated", "guild_id": guild_id, "enabled": enabled})


# --- Scrape Routes ---

# Single shared scraper instance — `PriceScraper` is stateless beyond its
# class-level config, so reusing one across requests saves a per-request
# allocation. `fetch()` itself is synchronous and blocking; that's fine
# under Flask's `threaded=True` because each request gets its own worker
# thread.
_price_scraper = PriceScraper()


@app.route("/wishlist/add", methods=["POST"])
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

    user_id = data["user_id"]
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

@app.route("/wishlist/remove", methods=["DELETE"])
@require_token
def api_remove_scrape():
    data = request.get_json()
    if not data or "user_id" not in data or "url" not in data:
        return jsonify({"error": "Missing user_id or url"}), 400

    success = delete_scraped_item(data["user_id"], data["url"])
    if success:
        logger.info(f"Scrape item removed via API for User {data['user_id']}")
        return jsonify({"status": "removed"})
    else:
        return jsonify({"error": "Item not found"}), 404

@app.route("/wishlist/all", methods=["GET"])
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


@app.route("/wishlist/preferences", methods=["GET"])
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


@app.route("/wishlist/preferences", methods=["PUT"])
@require_token
def api_set_wishlist_preferences():
    data = request.get_json()
    if not isinstance(data, dict) or not _is_int(data.get("user_id")) or not isinstance(
        data.get("url"), str
    ):
        return jsonify({"error": "user_id (integer) and url (string) are required"}), 400
    user_id = data["user_id"]
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


@app.route("/wishlist/refresh", methods=["POST"])
@require_token
def api_refresh_wishlist_item():
    """Refresh data only; this route never sends a Discord DM or LLM request."""
    data = request.get_json()
    if not isinstance(data, dict) or not _is_int(data.get("user_id")) or not isinstance(
        data.get("url"), str
    ):
        return jsonify({"error": "user_id (integer) and url (string) are required"}), 400
    item = get_scraped_item(data["user_id"], data["url"])
    if item is None:
        return jsonify({"error": "Item not found"}), 404

    result = _price_scraper.fetch(item[2])
    status = result.failure or "ok"
    update_scraped_item_check_status(item[0], status)
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

    if result.price is not None and result.price != item[3]:
        add_price_history(item[0], result.price)
    update_scraped_item_status(
        item[0], result.price, result.in_stock, result.title, result.currency
    )
    refreshed = _serialize_scraped_item(get_scraped_item(data["user_id"], data["url"]))
    refreshed["source"] = _domain(item[2])
    return jsonify(refreshed)


# --- Flight tracker configuration ------------------------------------------

@app.route("/flights/credentials", methods=["GET"])
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


@app.route("/flights/credentials", methods=["POST"])
@require_token
def api_set_flight_credentials():
    data = request.get_json()
    if not isinstance(data, dict) or not _is_int(data.get("user_id")):
        return jsonify({"error": "user_id must be an integer"}), 400
    api_key = data.get("api_key")
    if not isinstance(api_key, str) or not 1 <= len(api_key.strip()) <= 200:
        return jsonify({"error": "api_key must be a non-empty string up to 200 characters"}), 400
    try:
        SerpApiFlightProvider(api_key=api_key.strip()).validate_credentials()
    except FlightProviderError as exc:
        return jsonify({"error": "Credential validation failed", "detail": str(exc)}), 400
    if not set_flight_api_credentials(data["user_id"], api_key.strip()):
        return jsonify({"error": "Could not store credentials"}), 500
    logger.info("Validated and stored flight credentials through API for user %s", data["user_id"])
    return jsonify({"status": "stored", "user_id": data["user_id"]}), 201


@app.route("/flights/credentials", methods=["DELETE"])
@require_token
def api_delete_flight_credentials():
    data = request.get_json()
    user_id = data.get("user_id") if isinstance(data, dict) else None
    if not _is_int(user_id):
        return jsonify({"error": "user_id must be an integer"}), 400
    if not delete_flight_api_credentials(user_id):
        return jsonify({"error": "Credentials not found"}), 404
    return jsonify({"status": "deleted", "user_id": user_id})


@app.route("/flights/trackers", methods=["GET"])
@require_token
def api_get_flight_trackers():
    user_id = request.args.get("user_id", type=int)
    if user_id is None:
        return jsonify({"error": "Missing user_id query parameter"}), 400
    return jsonify({"user_id": user_id, "trackers": get_user_flight_trackers(user_id)})


@app.route("/flights/trackers", methods=["POST"])
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


@app.route("/flights/trackers/<int:tracker_id>", methods=["GET"])
@require_token
def api_get_flight_tracker(tracker_id):
    user_id = request.args.get("user_id", type=int)
    if user_id is None:
        return jsonify({"error": "Missing user_id query parameter"}), 400
    tracker = get_flight_tracker(tracker_id, user_id)
    if tracker is None:
        return jsonify({"error": "Tracker not found"}), 404
    return jsonify(tracker)


@app.route("/flights/trackers/<int:tracker_id>", methods=["DELETE"])
@require_token
def api_delete_flight_tracker(tracker_id):
    data = request.get_json()
    user_id = data.get("user_id") if isinstance(data, dict) else None
    if not _is_int(user_id):
        return jsonify({"error": "user_id must be an integer"}), 400
    if not delete_flight_tracker(user_id, tracker_id):
        return jsonify({"error": "Tracker not found"}), 404
    return jsonify({"status": "deleted", "id": tracker_id})


@app.route("/flights/trackers/<int:tracker_id>/history", methods=["GET"])
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

@app.route("/settings/<key>", methods=["GET"])
@require_token
def api_get_setting(key):
    value = get_setting(key)
    if value is None:
        return jsonify({"error": "Setting not found"}), 404
    return jsonify({"key": key, "value": value})


@app.route("/settings/<key>", methods=["PUT"])
@require_token
def api_set_setting(key):
    data = request.get_json()
    if not data or "value" not in data:
        return jsonify({"error": "Missing 'value' field"}), 400

    set_setting(key, data["value"])
    logger.info(f"Setting '{key}' updated via API from {request.remote_addr}")
    return jsonify({"status": "updated", "key": key, "value": data["value"]})


if __name__ == "__main__":
    logger.info(f"Starting Flask API Server on {HOST}:{PORT}")
    app.run(host=HOST, port=int(PORT), threaded=True)
