import os
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from db import (
    init_db, add_response, remove_response, get_all_responses,
    add_reminder, get_due_reminders, delete_reminder
)

load_dotenv()

HOST = os.getenv('HOST', '0.0.0.0')
PORT = os.getenv('PORT', 5000)

app = Flask(__name__)
init_db()

# --- Response Routes ---

@app.route("/add", methods=["POST"])
def add():
    data = request.get_json()
    if not data or "keyword" not in data or "response" not in data:
        return jsonify({"error": "Invalid payload"}), 400

    add_response(data["keyword"], data["response"])
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
        return jsonify({"status": "removed"})
    else:
        return jsonify({"error": "Keyword or response not found"}), 404

@app.route("/all", methods=["GET"])
def all_responses():
    responses = get_all_responses()
    return jsonify(responses)

# --- Reminder Routes ---

@app.route("/reminders/add", methods=["POST"])
def api_add_reminder():
    data = request.get_json()
    required = ["user_id", "channel_id", "remind_at", "message"]
    if not data or not all(k in data for k in required):
        return jsonify({"error": "Missing required fields"}), 400

    add_reminder(
        data["user_id"], 
        data["channel_id"], 
        data["remind_at"], 
        data["message"]
    )
    return jsonify({"status": "reminder_set"})

@app.route("/reminders/due", methods=["GET"])
def api_get_due():
    # Fetches all reminders where remind_at <= current time
    reminders = get_due_reminders()
    # Format the tuples from SQLite into a clean list of dicts
    result = [
        {"id": r[0], "user_id": r[1], "channel_id": r[2], "message": r[3]} 
        for r in reminders
    ]
    return jsonify(result)

@app.route("/reminders/delete/<int:reminder_id>", methods=["DELETE"])
def api_delete_reminder(reminder_id):
    delete_reminder(reminder_id)
    return jsonify({"status": "deleted", "id": reminder_id})

if __name__ == "__main__":
    app.run(host=HOST, port=int(PORT))
