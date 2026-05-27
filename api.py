import os
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from db import (
    init_db, add_response, remove_response, get_all_responses,
    add_reminder, delete_reminder, get_all_reminders,
    add_joke, get_all_jokes, get_joke_by_id, update_joke, delete_joke, reset_jokes,
    add_scraped_item, delete_scraped_item, get_all_scraped_items
)
from logger import setup_logger
import logging

logger = setup_logger("flask_api", "api.log")

load_dotenv()

HOST = os.getenv('HOST')
PORT = os.getenv('PORT')

app = Flask(__name__)

# Reduce Flask's default verbose logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

init_db()

# --- Response Routes ---

@app.route("/add", methods=["POST"])
def add():
    data = request.get_json()
    if not data or "keyword" not in data or "response" not in data:
        logger.warning(f"BadRequest: Missing fields in /add. IP: {request.remote_addr}")
        return jsonify({"error": "Invalid payload"}), 400

    add_response(data["keyword"], data["response"])
    logger.info(f"Keyword added via API: '{data['keyword']}' from {request.remote_addr}")
    return jsonify({"status": "ok"})

@app.route("/remove", methods=["DELETE"])
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
def all_responses():
    logger.info(f"Fetching all responses. Requested by {request.remote_addr}")
    responses = get_all_responses()
    return jsonify(responses)

# --- Reminder Routes ---

@app.route("/reminders/add", methods=["POST"])
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
def api_delete_reminder(reminder_id):
    delete_reminder(reminder_id)
    logger.info(f"Reminder {reminder_id} deleted via API")
    return jsonify({"status": "deleted", "id": reminder_id})

@app.route("/reminders/all", methods=["GET"]) 
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
def api_get_joke(joke_id):
    joke = get_joke_by_id(joke_id)
    if joke is None:
        return jsonify({"error": "Joke not found"}), 404
    return jsonify({"id": joke[0], "text": joke[1], "sent": bool(joke[2])})

@app.route("/jokes", methods=["POST"])
def api_add_joke():
    data = request.get_json()
    if not data or "text" not in data:
        logger.warning(f"BadRequest: Missing 'text' in POST /jokes. IP: {request.remote_addr}")
        return jsonify({"error": "Missing 'text' field"}), 400

    add_joke(data["text"])
    logger.info(f"Joke added via API from {request.remote_addr}")
    return jsonify({"status": "ok"}), 201

@app.route("/jokes/<int:joke_id>", methods=["PUT"])
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
def api_delete_joke(joke_id):
    success = delete_joke(joke_id)
    if success:
        logger.info(f"Joke {joke_id} deleted via API")
        return jsonify({"status": "deleted", "id": joke_id})
    else:
        return jsonify({"error": "Joke not found"}), 404

@app.route("/jokes/reset", methods=["POST"])
def api_reset_jokes():
    reset_jokes()
    logger.info("All jokes reset to unsent via API")
    return jsonify({"status": "reset"})

# --- Scrape Routes ---

@app.route("/scrape/add", methods=["POST"])
def api_add_scrape():
    data = request.get_json()
    if not data or "user_id" not in data or "url" not in data:
        logger.warning(f"BadRequest: Missing fields in /scrape/add. IP: {request.remote_addr}")
        return jsonify({"error": "Missing user_id or url"}), 400

    add_scraped_item(data["user_id"], data["url"])
    logger.info(f"Scrape item added via API for User {data['user_id']}: {data['url']}")
    return jsonify({"status": "ok"})

@app.route("/scrape/remove", methods=["DELETE"])
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
                "title": i[5] if len(i) > 5 else None
            } for i in items
        ]
        return jsonify(result)
    except Exception:
        logger.exception("Error in /scrape/all")
        return jsonify({"error": "Internal server error"}), 500

if __name__ == "__main__":
    logger.info(f"Starting Flask API Server on {HOST}:{PORT}")
    app.run(host=HOST, port=int(PORT), threaded=True)
