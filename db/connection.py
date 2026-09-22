"""SQLite connection ownership shared by all persistence modules."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

DB_FILE = os.getenv("DB_FILE", "responses.db")
_db_dir = os.path.dirname(DB_FILE)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)


@contextmanager
def _connect(commit=False):
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn.cursor()
        if commit:
            conn.commit()
    finally:
        conn.close()

