"""Bearer-token checks shared by API blueprints."""

from __future__ import annotations

import hmac
import logging
from functools import wraps

from flask import current_app, jsonify, request

from web.helpers import valid_json_value

logger = logging.getLogger("flask_api")


def require_token(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        token = current_app.config.get("API_TOKEN")
        if not isinstance(token, str) or not token.strip():
            return jsonify({"error": "API_TOKEN configuration is required"}), 503
        auth = request.headers.get("Authorization", "")
        prefix = "Bearer "
        if not auth.startswith(prefix) or not hmac.compare_digest(
            auth[len(prefix):].encode("utf-8"), token.encode("utf-8")
        ):
            logger.warning(
                "Unauthorized request to %s from %s",
                request.path,
                request.remote_addr,
            )
            return jsonify({"error": "Unauthorized"}), 401
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.is_json:
            data = request.get_json(silent=True)
            if not isinstance(data, dict) or not valid_json_value(data):
                return jsonify({"error": "A JSON object with finite values is required"}), 400
        return func(*args, **kwargs)

    return wrapper


def require_analytics_token(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        token = current_app.config.get("API_TOKEN")
        if not isinstance(token, str) or not token.strip():
            return jsonify({"error": "Analytics requires API_TOKEN configuration"}), 503
        auth = request.headers.get("Authorization", "")
        prefix = "Bearer "
        if not auth.startswith(prefix) or not hmac.compare_digest(
            auth[len(prefix):].encode("utf-8"), token.encode("utf-8")
        ):
            return jsonify({"error": "Unauthorized"}), 401
        return func(*args, **kwargs)

    return wrapper
