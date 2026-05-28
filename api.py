import hmac
import logging
import os
import sys
import threading
from functools import wraps

from dotenv import load_dotenv
from flask import Flask, jsonify, request

from db import (
    add_joke,
    add_reminder,
    add_response,
    add_scraped_item,
    delete_joke,
    delete_reminder,
    delete_scraped_item,
    get_all_jokes,
    get_all_reminders,
    get_all_responses,
    get_all_scraped_items,
    get_joke_by_id,
    init_db,
    remove_response,
    reset_jokes,
    update_joke,
)
from logger import setup_logger

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
    reset_jokes()
    logger.info("All jokes reset to unsent via API")
    return jsonify({"status": "reset"})

# --- Scrape Routes ---

@app.route("/scrape/add", methods=["POST"])
@require_token
def api_add_scrape():
    data = request.get_json()
    if not data or "user_id" not in data or "url" not in data:
        logger.warning(f"BadRequest: Missing fields in /scrape/add. IP: {request.remote_addr}")
        return jsonify({"error": "Missing user_id or url"}), 400

    add_scraped_item(data["user_id"], data["url"])
    logger.info(f"Scrape item added via API for User {data['user_id']}: {data['url']}")
    return jsonify({"status": "ok"})

@app.route("/scrape/remove", methods=["DELETE"])
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

@app.route("/scrape/all", methods=["GET"])
@require_token
def api_get_all_scrapes():
    try:
        items = get_all_scraped_items()
        result = [
            {
                "id": i[0],
                "user_id": i[1],
                "url": i[2],
                "last_price": i[3],
                "in_stock": bool(i[4]),
                "title": i[5] if len(i) > 5 else None,
                "currency": i[6] if len(i) > 6 else None
            } for i in items
        ]
        return jsonify(result)
    except Exception:
        logger.exception("Error in /scrape/all")
        return jsonify({"error": "Internal server error"}), 500

if __name__ == "__main__":
    logger.info(f"Starting Flask API Server on {HOST}:{PORT}")
    app.run(host=HOST, port=int(PORT), threaded=True)
