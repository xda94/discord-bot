import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logger(name, log_file):
    log_dir = os.getenv("LOG_DIR")
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        log_file = str(Path(log_dir) / Path(log_file).name)

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'
    )

    file_handler = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=2)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # Attach the shared module loggers ("database", "scraper") to the same
    # handlers so their output ends up in `bot.log` / `api.log` instead of
    # being silently dropped by Python's default root handler config.
    for child_name in ("database", "scraper"):
        child = logging.getLogger(child_name)
        child.setLevel(logging.INFO)
        child.addHandler(file_handler)
        child.addHandler(console_handler)

    return logger
