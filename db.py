import sqlite3
import random
import time
import logging

# We do NOT configure basicConfig here. We just get a logger.
logger = logging.getLogger("database")

DB_FILE = "responses.db"

def init_db():
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS responses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL,
                response TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                remind_at REAL NOT NULL,
                message TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS keyword_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                used_at REAL NOT NULL
            )
        """)
        conn.commit()
        conn.close()
        logger.info("Database connection initialized.")
    except Exception:
        logger.exception("Critical error initializing database")

def add_response(keyword, response):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            "INSERT INTO responses (keyword, response) VALUES (?, ?)",
            (keyword.lower(), response)
        )
        conn.commit()
        conn.close()
        logger.info(f"Inserted new response for keyword: '{keyword}'")
    except Exception:
        logger.exception(f"Failed to add response for '{keyword}'")

def remove_response(keyword, response=None):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()

        if response is None:
            c.execute('DELETE FROM responses WHERE keyword = ?', (keyword.lower(),))
        else:
            c.execute('DELETE FROM responses WHERE keyword = ? AND response = ?',
                      (keyword.lower(), response))

        deleted = c.rowcount
        conn.commit()
        conn.close()
        logger.info(f"Deleted {deleted} response(s) for keyword: '{keyword}'")
        return deleted > 0
    except Exception:
        logger.exception(f"Failed to remove response for '{keyword}'")
        return False

def get_all_responses():
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute('SELECT keyword, response FROM responses')
        data = c.fetchall()
        conn.close()
        
        result = {}
        for keyword, response in data:
            key = keyword.lower()
            if key not in result:
                result[key] = []
            result[key].append(response)
        return result
    except Exception:
        logger.exception("Failed to fetch all responses")
        return {}

def get_random_response(keyword):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            "SELECT response FROM responses WHERE keyword = ?",
            (keyword.lower(),)
        )
        rows = c.fetchall()
        conn.close()
        if not rows:
            return None
        return random.choice(rows)[0]
    except Exception:
        logger.exception(f"Error retrieving random response for '{keyword}'")
        return None

def add_reminder(user_id, channel_id, remind_at, message):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            "INSERT INTO reminders (user_id, channel_id, remind_at, message) VALUES (?, ?, ?, ?)",
            (user_id, channel_id, remind_at, message)
        )
        conn.commit()
        conn.close()
        logger.info(f"Scheduled reminder for user {user_id} at timestamp {remind_at}")
    except Exception:
        logger.exception("Failed to add reminder")

def get_due_reminders():
    try:
        now = time.time()
        # Use context manager for automatic closing
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            # Log 'now' to compare against what's in your DB manually
            logger.debug(f"Checking for reminders due before: {now}")
            c.execute("SELECT id, user_id, channel_id, message FROM reminders WHERE remind_at <= ?", (now,))
            rows = c.fetchall()
            return rows
    except Exception:
        logger.exception("Failed to fetch due reminders")
        return []

def delete_reminder(reminder_id):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        conn.commit()
        conn.close()
        logger.debug(f"Deleted executed reminder ID: {reminder_id}")
    except Exception:
        logger.exception(f"Failed to delete reminder ID {reminder_id}")

def get_all_reminders():
    try:
        # Use context manager for automatic closing
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            logger.debug("Fetching all reminders from the database.")

            # Select all columns without a filter
            c.execute("SELECT id, user_id, channel_id, message, remind_at FROM reminders")

            rows = c.fetchall()
            return rows
    except Exception:
        logger.exception("Failed to fetch all reminders")
        return []

def log_keyword_usage(keyword, user_id, guild_id):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            "INSERT INTO keyword_usage (keyword, user_id, guild_id, used_at) VALUES (?, ?, ?, ?)",
            (keyword.lower(), user_id, guild_id, time.time())
        )
        conn.commit()
        conn.close()
        logger.debug(f"Logged usage of keyword '{keyword}' by user {user_id}")
    except Exception:
        logger.exception(f"Failed to log keyword usage for '{keyword}'")

def get_top_keywords(guild_id, limit=10):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            "SELECT keyword, COUNT(*) as cnt FROM keyword_usage WHERE guild_id = ? GROUP BY keyword ORDER BY cnt DESC LIMIT ?",
            (guild_id, limit)
        )
        rows = c.fetchall()
        conn.close()
        logger.info(f"Fetched top {limit} keywords for guild {guild_id}")
        return rows
    except Exception:
        logger.exception("Failed to fetch top keywords")
        return []

def get_top_keywords_by_user(guild_id, user_id, limit=10):
    try:
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute(
            "SELECT keyword, COUNT(*) as cnt FROM keyword_usage WHERE guild_id = ? AND user_id = ? GROUP BY keyword ORDER BY cnt DESC LIMIT ?",
            (guild_id, user_id, limit)
        )
        rows = c.fetchall()
        conn.close()
        logger.info(f"Fetched top {limit} keywords for user {user_id} in guild {guild_id}")
        return rows
    except Exception:
        logger.exception(f"Failed to fetch top keywords for user {user_id}")
        return []
