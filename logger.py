import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote, quote_plus


def redact_sensitive(value, secrets=()):
    text = str(value)
    configured = [
        item for name, item in os.environ.items()
        if name.endswith(("_TOKEN", "_KEY", "_SECRET", "_PASSWORD")) and item
    ]
    for secret in sorted(set(configured).union(item for item in secrets if item), key=len, reverse=True):
        for encoded in {secret, quote(secret, safe=""), quote_plus(secret)}:
            text = text.replace(encoded, "[REDACTED]")
    key_pattern = r"(?i)([\"']?\b(?:api_key|access_token|token|password|secret)[\"']?\s*[=:]\s*)"
    text = re.sub(
        key_pattern + r'''("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')''',
        lambda match: match[1] + match[2][0] + "[REDACTED]" + match[2][-1],
        text,
    )
    text = re.sub(
        key_pattern + r"[^\s&\"'<>]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1[REDACTED]@", text)
    return re.sub(r"(?i)(\bBearer\s+)[^\s\"'<>]+", r"\1[REDACTED]", text)


class RedactingFormatter(logging.Formatter):
    def formatException(self, exc_info):
        return redact_sensitive(super().formatException(exc_info))

    def format(self, record):
        return redact_sensitive(super().format(record))


def setup_logger(name, log_file):
    log_dir = os.getenv("LOG_DIR")
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        log_file = str(Path(log_dir) / Path(log_file).name)

    logger = logging.getLogger(name)
    configured_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    level = getattr(logging, configured_level, None)
    if not isinstance(level, int):
        level = logging.INFO
        configured_level = "INFO"
    logger.setLevel(level)

    formatter = RedactingFormatter(
        "%(asctime)s - %(levelname)s - [%(name)s pid=%(process)d "
        "thread=%(threadName)s %(filename)s:%(lineno)d] - %(message)s"
    )

    file_handler = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=2)
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # Attach the shared module loggers ("database", "scraper") to the same
    # handlers so their output ends up in `bot.log` / `api.log` instead of
    # being silently dropped by Python's default root handler config.
    for child_name in ("database", "scraper"):
        child = logging.getLogger(child_name)
        child.setLevel(level)
        child.addHandler(file_handler)
        child.addHandler(console_handler)

    logger.info(
        "Logger initialized name=%s level=%s file=%s",
        name,
        configured_level,
        log_file,
    )
    return logger
