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

    # Attach db logger to the same handlers
    db_logger = logging.getLogger("database")
    db_logger.setLevel(logging.INFO)
    db_logger.addHandler(file_handler)
    db_logger.addHandler(console_handler)

    return logger
