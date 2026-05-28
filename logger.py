import logging
from logging.handlers import RotatingFileHandler


def setup_logger(name, log_file):
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
