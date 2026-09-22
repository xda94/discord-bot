"""Bearer-token checks shared by API blueprints."""

from __future__ import annotations

import hmac
import logging
from functools import wraps

from flask import current_app, jsonify, request

logger = logging.getLogger("flask_api")


def require_token(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        token = current_app.config.get("API_TOKEN")
        if not token:
            return func(*args, **kwargs)
        auth = request.headers.get("Authorization", "")
        prefix = "Bearer "
        if not auth.startswith(prefix) or not hmac.compare_digest(
            auth[len(prefix):], token
        ):
            logger.warning(
                "Unauthorized request to %s from %s",
                request.path,
                request.remote_addr,
            )
            return jsonify({"error": "Unauthorized"}), 401
        return func(*args, **kwargs)

    return wrapper


def require_analytics_token(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        token = current_app.config.get("API_TOKEN")
        if not token:
            return jsonify({"error": "Analytics requires API_TOKEN configuration"}), 503
        auth = request.headers.get("Authorization", "")
        prefix = "Bearer "
        if not auth.startswith(prefix) or not hmac.compare_digest(
            auth[len(prefix):], token
        ):
            return jsonify({"error": "Unauthorized"}), 401
        return func(*args, **kwargs)

    return wrapper

