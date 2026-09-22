"""Executable entry point for the Flask administration API."""

import logging
import os
import sys

from dotenv import load_dotenv

from logger import setup_logger

logger = setup_logger("flask_api", "api.log")
load_dotenv()

HOST = os.getenv("HOST")
PORT = os.getenv("PORT")
API_TOKEN = os.getenv("API_TOKEN")

_missing = [name for name, value in (("HOST", HOST), ("PORT", PORT)) if not value]
if _missing:
    logger.critical(
        "Required env var(s) missing: %s. Set them in your .env file and restart. "
        "Refusing to start.",
        ", ".join(_missing),
    )
    sys.exit(1)
try:
    int(PORT)
except (TypeError, ValueError):
    logger.critical("PORT is not a valid integer (got %r). Refusing to start.", PORT)
    sys.exit(1)

logging.getLogger("werkzeug").setLevel(logging.ERROR)

from web import create_app  # noqa: E402

app = create_app({"API_TOKEN": API_TOKEN})


if __name__ == "__main__":
    logger.info("Starting Flask API Server on %s:%s", HOST, PORT)
    app.run(host=HOST, port=int(PORT), threaded=True)
