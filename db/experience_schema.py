"""Idempotent additive migrations for conversational actions and delivery."""
from __future__ import annotations


def migrate(c):
    def add(table, columns):
        existing = {r[1] for r in c.execute('PRAGMA table_info(' + table + ')')}
        for name, definition in columns.items():
            if name not in existing:
                c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
    add('assistant_profiles', {
        'timezone_configured':'INTEGER NOT NULL DEFAULT 0',
        'quiet_start':'TEXT', 'quiet_end':'TEXT',
        'delivery_mode':"TEXT NOT NULL DEFAULT 'immediate'",
        'digest_time':"TEXT NOT NULL DEFAULT '09:00'",
    })
    c.execute("UPDATE assistant_profiles SET timezone_configured=1 WHERE timezone != 'UTC'")
    add('reminders', {
        'creator_id':'INTEGER', 'timezone':"TEXT NOT NULL DEFAULT 'UTC'",
        'language':"TEXT NOT NULL DEFAULT 'en'", 'recurrence':'TEXT',
        'state':"TEXT NOT NULL DEFAULT 'pending'", 'attempt_count':'INTEGER NOT NULL DEFAULT 0',
        'next_attempt':'REAL', 'last_error':'TEXT', 'claimed_at':'REAL',
        'finished_at':'REAL', 'discord_message_id':'INTEGER', 'revision':'INTEGER NOT NULL DEFAULT 0',
    })
    c.execute('UPDATE reminders SET creator_id=user_id WHERE creator_id IS NULL')
    c.execute('CREATE INDEX IF NOT EXISTS reminders_due ON reminders(state, remind_at, next_attempt)')
    c.execute('''CREATE TABLE IF NOT EXISTS reminder_occurrences (
      id INTEGER PRIMARY KEY AUTOINCREMENT, reminder_id INTEGER NOT NULL,
      scheduled_at REAL NOT NULL, state TEXT NOT NULL, discord_message_id INTEGER,
      last_error TEXT, UNIQUE(reminder_id, scheduled_at))''')
    add('scraped_items', {'language': "TEXT NOT NULL DEFAULT 'en'"})
    add('flight_trackers', {'language': "TEXT NOT NULL DEFAULT 'en'", 'budget':'REAL', 'budget_alerted':'INTEGER NOT NULL DEFAULT 0'})
    c.execute('''CREATE TABLE IF NOT EXISTS action_executions (
      event_id TEXT PRIMARY KEY, state TEXT NOT NULL, created_at REAL NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS notification_outbox (
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
      source TEXT NOT NULL, item_key TEXT NOT NULL, event_kind TEXT NOT NULL,
      body TEXT NOT NULL, language TEXT NOT NULL DEFAULT 'en', observed_at REAL NOT NULL,
      due_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
      last_error TEXT, claimed_at REAL, finished_at REAL)''')
    add('notification_outbox', {'sent_parts': 'INTEGER NOT NULL DEFAULT 0', 'discord_message_id': 'INTEGER', 'protected_json': "TEXT NOT NULL DEFAULT '[]'", 'delivery_body': 'TEXT'})
    c.execute('CREATE INDEX IF NOT EXISTS notifications_due ON notification_outbox(state,due_at)')
