import hmac
import logging
import os
import sys
import threading
from datetime import datetime
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
    delete_joke,
    delete_reminder,
    delete_scraped_item,
    get_all_guild_joke_configs,
    get_all_jokes,
    get_all_reminders,
    get_all_responses,
    get_all_scraped_items,
    get_guild_joke_config,
    get_joke_by_id,
    init_db,
    remove_response,
    reset_all_guild_joke_sent,
    set_guild_joke_config,
    update_joke,
)
from logger import setup_logger
from scraper import (
    FAILURE_BLOCKED,
    FAILURE_UNSUPPORTED,
    PriceScraper,
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

# --- Response Routes ---

@app.route("/add", methods=["POST"])
@require_token
def add():
    data = request.get_json()
    if not data or "keyword" not in data or "response" not in data:
        logger.warning(f"BadRequest: Missing fields in /add. IP: {request.remote_addr}")
        return jsonify({"error": "Invalid payload"}), 400

    add_response(data["keyword"], data["response"])
    logger.info(f"Keyword added via API: '{data['keyword']}' from {request.remote_addr}")
    return jsonify({"status": "ok"})

@app.route("/remove", methods=["DELETE"])
@require_token
def remove():
    data = request.get_json()
    if not data or "keyword" not in data:
        return jsonify({"error": "Invalid payload"}), 400

    keyword = data["keyword"]
    response = data.get("response") 

    success = remove_response(keyword, response)
    if success:
        logger.info(f"Keyword removed via API: '{keyword}'")
        return jsonify({"status": "removed"})
    else:
        logger.warning(f"Failed remove request: '{keyword}' not found.")
        return jsonify({"error": "Keyword or response not found"}), 404

@app.route("/all", methods=["GET"])
@require_token
def all_responses():
    logger.info(f"Fetching all responses. Requested by {request.remote_addr}")
    responses = get_all_responses()
    return jsonify(responses)

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
# `GET /jokes/guilds` — list every guild that has /joke_activation set
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
        # `get_all_scraped_items` returns a 9-tuple:
        #   (id, user_id, url, last_price, last_stock_status, title,
        #    currency, last_alert_kind, last_alert_price)
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
            } for i in items
        ]
        return jsonify(result)
    except Exception:
        logger.exception("Error in /wishlist/all")
        return jsonify({"error": "Internal server error"}), 500

if __name__ == "__main__":
    logger.info(f"Starting Flask API Server on {HOST}:{PORT}")
    app.run(host=HOST, port=int(PORT), threaded=True)
