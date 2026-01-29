import sqlite3
import random
import time

DB_FILE = "responses.db"

def init_db():
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
    conn.commit()
    conn.close()

def add_response(keyword, response):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO responses (keyword, response) VALUES (?, ?)",
        (keyword.lower(), response)
    )
    conn.commit()
    conn.close()

def remove_response(keyword, response=None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    if response is None:
        # Delete all responses for the keyword
        c.execute('DELETE FROM responses WHERE keyword = ?', (keyword.lower(),))
    else:
        # Delete only the specific response
        c.execute('DELETE FROM responses WHERE keyword = ? AND response = ?',
                  (keyword.lower(), response))

    deleted = c.rowcount
    conn.commit()
    conn.close()
    return deleted > 0

def get_all_responses():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('SELECT keyword, response FROM responses')
    data = c.fetchall()
    conn.close()
    
    result = {}
    for keyword, response in data:
        key = keyword.lower()  # ensure consistent lowercase keys
        if key not in result:
            result[key] = []
        result[key].append(response)
    return result

def get_random_response(keyword):
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

def add_reminder(user_id, channel_id, remind_at, message):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO reminders (user_id, channel_id, remind_at, message) VALUES (?, ?, ?, ?)",
        (user_id, channel_id, remind_at, message)
    )
    conn.commit()
    conn.close()

def get_due_reminders():
    now = time.time()
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, user_id, channel_id, message FROM reminders WHERE remind_at <= ?", (now,))
    rows = c.fetchall()
    conn.close()
    return rows

def delete_reminder(reminder_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
    conn.commit()
    conn.close()
