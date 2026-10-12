from __future__ import annotations


def migrate(c):
    c.execute('''CREATE TABLE IF NOT EXISTS community_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL CHECK (guild_id > 0),
        channel_id INTEGER NOT NULL CHECK (channel_id > 0),
        creator_id INTEGER NOT NULL CHECK (creator_id > 0),
        subject_id INTEGER CHECK (subject_id > 0),
        kind TEXT NOT NULL CHECK (kind IN ('tribunal','sedinta','quest','quiz')),
        phase TEXT NOT NULL CHECK (phase IN ('pending','active','verification','closing','closed','cancelled','declined','expired')),
        language TEXT NOT NULL CHECK (language IN ('en','ro')),
        payload TEXT NOT NULL CHECK (length(payload) <= 6000),
        message_id INTEGER CHECK (message_id > 0),
        created_at REAL NOT NULL, deadline REAL NOT NULL, closed_at REAL,
        result_text TEXT CHECK (length(result_text) <= 1900),
        generation_started_at REAL,
        delivery_status TEXT NOT NULL DEFAULT 'pending' CHECK (delivery_status IN ('pending','delivered','retry','error')),
        retry_at REAL, error_code TEXT CHECK (error_code IN ('not_found','forbidden','http','database','cancelled','quote_removed') OR error_code IS NULL)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS community_votes (
        session_id INTEGER NOT NULL REFERENCES community_sessions(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL CHECK (user_id > 0),
        option_key TEXT NOT NULL CHECK (length(option_key) BETWEEN 1 AND 80),
        updated_at REAL NOT NULL, PRIMARY KEY (session_id,user_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS community_titles (
        guild_id INTEGER NOT NULL CHECK (guild_id > 0), user_id INTEGER NOT NULL CHECK (user_id > 0),
        title_key TEXT NOT NULL CHECK (length(title_key) BETWEEN 1 AND 80),
        awarded_at REAL NOT NULL, session_id INTEGER REFERENCES community_sessions(id) ON DELETE SET NULL,
        PRIMARY KEY (guild_id,user_id,title_key)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS bingo_habits (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL CHECK (guild_id > 0),
        text_en TEXT NOT NULL CHECK (length(text_en) BETWEEN 1 AND 200),
        text_ro TEXT NOT NULL CHECK (length(text_ro) BETWEEN 1 AND 200),
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1))
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS bingo_boards (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL CHECK (guild_id > 0),
        week_key TEXT NOT NULL, cells TEXT NOT NULL CHECK (length(cells) <= 6000),
        created_at REAL NOT NULL, UNIQUE (guild_id,week_key)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS bingo_marks (
        board_id INTEGER NOT NULL REFERENCES bingo_boards(id) ON DELETE CASCADE,
        cell_index INTEGER NOT NULL CHECK (cell_index BETWEEN 0 AND 8),
        marker_id INTEGER NOT NULL CHECK (marker_id > 0),
        confirmer_id INTEGER CHECK (confirmer_id > 0 AND confirmer_id != marker_id),
        marked_at REAL NOT NULL, confirmed_at REAL, PRIMARY KEY (board_id,cell_index)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS museum_quotes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL CHECK (guild_id > 0),
        source_channel_id INTEGER NOT NULL CHECK (source_channel_id > 0),
        source_message_id INTEGER NOT NULL CHECK (source_message_id > 0),
        nominator_id INTEGER NOT NULL CHECK (nominator_id > 0), author_id INTEGER NOT NULL CHECK (author_id > 0),
        quote_text TEXT NOT NULL CHECK (length(quote_text) <= 1200),
        author_name TEXT NOT NULL CHECK (length(author_name) BETWEEN 1 AND 100),
        language TEXT NOT NULL CHECK (language IN ('en','ro')),
        status TEXT NOT NULL CHECK (status IN ('pending','approved')),
        created_at REAL NOT NULL, consent_expires_at REAL NOT NULL, approved_at REAL,
        consent_message_id INTEGER CHECK (consent_message_id > 0),
        consent_channel_id INTEGER CHECK (consent_channel_id > 0),
        UNIQUE (guild_id,source_message_id)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS community_sessions_due ON community_sessions(phase,deadline,retry_at)')
    c.execute('CREATE INDEX IF NOT EXISTS community_sessions_guild ON community_sessions(guild_id,id DESC)')
    c.execute('DROP INDEX IF EXISTS community_active_quest')
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS community_active_quest_recipient ON community_sessions(guild_id,COALESCE(subject_id,creator_id)) WHERE kind='quest' AND phase IN ('pending','active','verification','closing')")
    c.execute('CREATE INDEX IF NOT EXISTS museum_quotes_guild ON museum_quotes(guild_id,status,id DESC)')
    c.execute('CREATE INDEX IF NOT EXISTS bingo_boards_week ON bingo_boards(guild_id,week_key)')
    c.execute('CREATE INDEX IF NOT EXISTS bingo_habits_guild ON bingo_habits(guild_id,active)')
