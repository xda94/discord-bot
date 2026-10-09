"""Flask application factory and process-wide request hooks."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from flask import Flask, jsonify, request

import db
from web.helpers import format_discord_ids

logger = logging.getLogger("flask_api")
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def create_app(config: dict | None = None) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(PROJECT_ROOT / "templates"),
        static_folder=str(PROJECT_ROOT / "static"),
        static_url_path="/static",
    )
    app.config["API_TOKEN"] = os.getenv("API_TOKEN")
    if config:
        app.config.update(config)

    if not app.config.get("API_TOKEN"):
        logger.critical(
            "API_TOKEN is not set. Protected API routes are unavailable until "
            "a bearer token is configured."
        )

    initialized = False
    init_lock = threading.Lock()

    @app.before_request
    def ensure_db_initialized():
        nonlocal initialized
        if initialized:
            return
        with init_lock:
            if not initialized:
                try:
                    db.init_db()
                except Exception:
                    logger.exception("Database initialization failed")
                    if request.endpoint == "health":
                        return jsonify({"status": "unavailable", "database": "unavailable"}), 503
                    return jsonify({"error": "Database is temporarily unavailable"}), 503
                initialized = True

    @app.get("/health")
    def health():
        try:
            with db.connection._connect() as cursor:
                cursor.execute("SELECT 1 FROM settings LIMIT 1")
                cursor.fetchone()
        except Exception:
            logger.exception("Database health check failed")
            return jsonify({"status": "unavailable", "database": "unavailable"}), 503
        return jsonify({"status": "ok", "database": "ok"})

    app.after_request(format_discord_ids)

    from web.routes.administration import blueprint as administration
    from web.routes.flights import blueprint as flights
    from web.routes.memory_llm import blueprint as memory_llm
    from web.routes.wishlist import blueprint as wishlist

    app.register_blueprint(administration)
    app.register_blueprint(memory_llm)
    app.register_blueprint(wishlist)
    app.register_blueprint(flights)
    return app
