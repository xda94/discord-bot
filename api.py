import os
import logging
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from db import (
    init_db, add_response, remove_response, get_all_responses,
    add_reminder, get_due_reminders, delete_reminder
)

# --- Enhanced Logging Setup for API ---
logger = logging.getLogger("flask_api")
logger.setLevel(logging.INFO)

formatter = logging.Formatter('%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s')

# Writes to api.log
file_handler = RotatingFileHandler('api.log', maxBytes=5*1024*1024, backupCount=2)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# Link DB logger
db_logger = logging.getLogger("database")
db_logger.setLevel(logging.INFO)
db_logger.addHandler(file_handler)
db_logger.addHandler(console_handler)
# --------------------------------------

load_dotenv()

HOST = os.getenv('HOST', '0.0.0.0')
PORT = os.getenv('PORT', 5000)

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

@app.route("/remove", methods=["POST"])
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

@app.route("/reminders/due", methods=["GET"])
def api_get_due():
    try:
        reminders = get_due_reminders()
        result = [
            {"id": r[0], "user_id": r[1], "channel_id": r[2], "message": r[3]} 
            for r in reminders
        ]
        return jsonify(result)
    except Exception:
        logger.exception("Error in /reminders/due")
        return jsonify({"error": "Internal server error"}), 500

@app.route("/reminders/delete/<int:reminder_id>", methods=["DELETE"])
def api_delete_reminder(reminder_id):
    delete_reminder(reminder_id)
    logger.info(f"Reminder {reminder_id} deleted via API")
    return jsonify({"status": "deleted", "id": reminder_id})

if __name__ == "__main__":
    logger.info(f"Starting Flask API Server on {HOST}:{PORT}")
    app.run(host=HOST, port=int(PORT))