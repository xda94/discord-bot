import sqlite3
import random

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
