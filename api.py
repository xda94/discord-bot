from flask import Flask, request, jsonify
from db import init_db, add_response, remove_response, get_all_responses

app = Flask(__name__)
init_db()

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
    response = data.get("response")  # optional

    success = remove_response(keyword, response)
    if success:
        return jsonify({"status": "removed"})
    else:
        return jsonify({"error": "Keyword or response not found"}), 404

@app.route("/all", methods=["GET"])
def all_responses():
    responses = get_all_responses()  # Returns a dict like {keyword: response, ...}
    return jsonify(responses)

if __name__ == "__main__":
    app.run(host="192.168.0.213", port=9999)
