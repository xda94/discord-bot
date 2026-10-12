from __future__ import annotations

import datetime
import hashlib
import json
import math
import random
import re
import time

from db.connection import _connect

ACTIVE_PHASES = ('pending', 'active', 'verification')
TERMINAL_PHASES = ('closed', 'cancelled', 'declined', 'expired')
KINDS = ('tribunal', 'sedinta', 'quest', 'quiz')
HABITS = (
    ('Someone says “just five minutes”', 'Cineva spune „doar cinci minute”'),
    ('A meeting could have been a message', 'O ședință putea fi un mesaj'),
    ('Someone forgets why they opened a tab', 'Cineva uită de ce a deschis o filă'),
    ('Coffee becomes a personality trait', 'Cafeaua devine o trăsătură de personalitate'),
    ('A game needs one more round', 'Un joc mai cere o rundă'),
    ('Someone celebrates a tiny victory', 'Cineva sărbătorește o mică victorie'),
    ('A typo starts a new word', 'O greșeală de tastare inventează un cuvânt'),
    ('Someone shares a harmless pun', 'Cineva spune un joc de cuvinte inofensiv'),
    ('A to-do list gains another list', 'O listă de sarcini primește încă o listă'),
    ('Someone remembers to drink water', 'Cineva își amintește să bea apă'),
    ('A snack improves the mood', 'O gustare îmbunătățește dispoziția'),
    ('Someone asks “is my microphone on?”', 'Cineva întreabă „merge microfonul?”'),
)


def _id(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 9223372036854775807:
        raise ValueError('invalid_id')
    return value


def _limit(value, maximum=100):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError('invalid_limit')
    return value


def _now(value):
    value = time.time() if value is None else value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('invalid_time')
    return float(value)


def _text(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError('invalid_text')
    return value.strip()


def _language(value):
    if value not in ('en', 'ro'):
        raise ValueError('invalid_language')
    return value


def _row(c, row):
    if row is None:
        return None
    result = dict(zip((column[0] for column in c.description), row))
    for key in ('payload', 'cells'):
        if key in result:
            result[key] = json.loads(result[key])
    return result


def _session(c, guild_id, session_id):
    c.execute('SELECT * FROM community_sessions WHERE guild_id=? AND id=?', (_id(guild_id), _id(session_id)))
    return _row(c, c.fetchone())


def _quote(c, guild_id, quote_id):
    c.execute('SELECT * FROM museum_quotes WHERE guild_id=? AND id=?', (_id(guild_id), _id(quote_id)))
    return _row(c, c.fetchone())


def _out(status, key, value=None, **extra):
    return dict(status=status, **{key: value}, **extra)


def _payload(kind, payload):
    if not isinstance(payload, dict):
        raise ValueError('invalid_payload')
    allowed = {
        'tribunal': {'accusation'},
        'sedinta': {'subject', 'options'},
        'quest': {'challenge_en', 'challenge_ro', 'title_key', 'title_en', 'title_ro'},
        'quiz': {'quote_id', 'quote_text', 'candidates', 'correct_author_id'},
    }[kind]
    if set(payload) != allowed:
        raise ValueError('invalid_payload')
    if kind == 'tribunal':
        _text(payload['accusation'], 500)
    elif kind == 'sedinta':
        _text(payload['subject'], 200)
        options = payload['options']
        if not isinstance(options, list) or not 2 <= len(options) <= 5:
            raise ValueError('invalid_options')
        normalized = [_text(option, 80).casefold() for option in options]
        if len(set(normalized)) != len(normalized):
            raise ValueError('invalid_options')
    elif kind == 'quest':
        for key in allowed:
            _text(payload[key], 80 if key == 'title_key' else 300)
        if not re.fullmatch(r'[a-z0-9_-]{1,80}', payload['title_key']):
            raise ValueError('invalid_title')
    else:
        _id(payload['quote_id'])
        _id(payload['correct_author_id'])
        _text(payload['quote_text'], 1200)
        candidates = payload['candidates']
        if not isinstance(candidates, list) or not 2 <= len(candidates) <= 4:
            raise ValueError('invalid_candidates')
        for candidate in candidates:
            if not isinstance(candidate, dict) or set(candidate) != {'user_id', 'name'}:
                raise ValueError('invalid_candidates')
            _id(candidate['user_id'])
            _text(candidate['name'], 100)
        ids = [candidate['user_id'] for candidate in candidates]
        if len(set(ids)) != len(ids) or payload['correct_author_id'] not in ids:
            raise ValueError('invalid_candidates')
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    if len(encoded) > 6000:
        raise ValueError('invalid_payload')
    return encoded


def create_session(guild_id, channel_id, creator_id, kind, language, payload, subject_id=None, now=None):
    guild_id, channel_id, creator_id = _id(guild_id), _id(channel_id), _id(creator_id)
    if kind not in KINDS:
        raise ValueError('invalid_kind')
    _language(language)
    encoded = _payload(kind, payload)
    if subject_id is not None:
        _id(subject_id)
    if kind == 'tribunal' and subject_id is None:
        raise ValueError('invalid_subject')
    with _connect(commit=True) as c:
        c.execute('BEGIN IMMEDIATE')
        now = _now(now)
        if kind == 'quest':
            c.execute("SELECT * FROM community_sessions WHERE guild_id=? AND creator_id=? AND kind='quest' AND phase IN ('pending','active','verification','closing') ORDER BY id LIMIT 1", (guild_id, creator_id))
            existing = _row(c, c.fetchone())
            if existing:
                existing['created'] = False
                return existing
        if kind == 'quiz':
            quote = _quote(c, guild_id, payload['quote_id'])
            if not quote or quote['status'] != 'approved' or quote['quote_text'] != payload['quote_text'] or quote['author_id'] != payload['correct_author_id']:
                raise ValueError('quote_unavailable')
        phase = 'pending' if kind == 'quest' else 'active'
        deadline = now + (86400 if phase == 'pending' else 600)
        c.execute('INSERT INTO community_sessions (guild_id,channel_id,creator_id,subject_id,kind,phase,language,payload,created_at,deadline) VALUES (?,?,?,?,?,?,?,?,?,?)', (guild_id, channel_id, creator_id, subject_id, kind, phase, language, encoded, now, deadline))
        result = _session(c, guild_id, c.lastrowid)
        result['created'] = True
        return result


def get_session(guild_id, session_id):
    with _connect() as c:
        return _session(c, guild_id, session_id)


def bind_session_message(guild_id, session_id, message_id):
    _id(message_id)
    with _connect(commit=True) as c:
        c.execute('BEGIN IMMEDIATE')
        session = _session(c, guild_id, session_id)
        if not session:
            return _out('missing', 'session')
        if session['phase'] not in ACTIVE_PHASES or session['message_id'] not in (None, message_id):
            return _out('closed', 'session', session)
        c.execute('UPDATE community_sessions SET message_id=? WHERE guild_id=? AND id=?', (message_id, guild_id, session_id))
        return _out('bound', 'session', _session(c, guild_id, session_id))


def _session_list(guild_id, where, params, limit, after_id=0):
    _limit(limit)
    if isinstance(after_id, bool) or not isinstance(after_id, int) or after_id < 0:
        raise ValueError('invalid_cursor')
    if guild_id is not None:
        where += ' AND guild_id=?'
        params.append(_id(guild_id))
    with _connect() as c:
        c.execute('SELECT * FROM community_sessions WHERE ' + where + ' AND id>? ORDER BY id LIMIT ?', (*params, after_id, limit))
        return [_row(c, row) for row in c.fetchall()]


def list_restorable_sessions(guild_id=None, limit=100, after_id=0):
    return _session_list(guild_id, "(phase IN ('pending','active','verification','closing') OR delivery_status IN ('pending','retry'))", [], limit, after_id)


def list_due_sessions(guild_id=None, now=None, limit=100, after_id=0):
    now = _now(now)
    return _session_list(guild_id, "((phase IN ('pending','active','verification') AND deadline<=?) OR (phase IN ('closing','closed','cancelled','declined','expired') AND delivery_status IN ('pending','retry') AND (retry_at IS NULL OR retry_at<=?)))", [now, now], limit, after_id)


def list_sessions(guild_id, limit=100):
    with _connect() as c:
        c.execute('SELECT * FROM community_sessions WHERE guild_id=? ORDER BY id DESC LIMIT ?', (_id(guild_id), _limit(limit)))
        return [_row(c, row) for row in c.fetchall()]


def transition_session(guild_id, session_id, expected_phase, new_phase, actor_id, now=None):
    _id(actor_id)
    with _connect(commit=True) as c:
        c.execute('BEGIN IMMEDIATE')
        now = _now(now)
        session = _session(c, guild_id, session_id)
        if not session:
            return _out('missing', 'session')
        owner = session['subject_id'] if session['kind'] == 'tribunal' else session['creator_id']
        if actor_id != owner:
            return _out('forbidden', 'session', session)
        if session['phase'] != expected_phase or now >= session['deadline']:
            return _out('closed', 'session', session)
        allowed = {('tribunal', 'pending', 'active'), ('tribunal', 'pending', 'declined'), ('quest', 'pending', 'active'), ('quest', 'pending', 'declined'), ('quest', 'active', 'verification'), ('quest', 'active', 'cancelled')}
        if (session['kind'], expected_phase, new_phase) not in allowed:
            return _out('invalid', 'session', session)
        if new_phase in ('active', 'verification') and not session['message_id']:
            return _out('unbound', 'session', session)
        deadline = now + (604800 if session['kind'] == 'quest' and new_phase == 'active' else 600)
        closed = now if new_phase in TERMINAL_PHASES else None
        c.execute('UPDATE community_sessions SET phase=?,deadline=?,closed_at=? WHERE guild_id=? AND id=? AND phase=?', (new_phase, deadline, closed, guild_id, session_id, expected_phase))
        return _out('changed', 'session', _session(c, guild_id, session_id))


def _options(session):
    if session['kind'] == 'tribunal':
        return ['guilty', 'innocent']
    if session['kind'] == 'quest':
        return ['success', 'not_yet']
    if session['kind'] == 'sedinta':
        return [str(i) for i in range(len(session['payload']['options']))]
    return [str(candidate['user_id']) for candidate in session['payload'].get('candidates', [])]


def cast_vote(guild_id, session_id, user_id, option_key, now=None):
    _id(user_id)
    with _connect(commit=True) as c:
        c.execute('BEGIN IMMEDIATE')
        now = _now(now)
        session = _session(c, guild_id, session_id)
        if not session:
            return _out('missing', 'session')
        phase = 'verification' if session['kind'] == 'quest' else 'active'
        if session['phase'] != phase or now >= session['deadline'] or session['message_id'] is None:
            return _out('closed', 'session', session)
        if session['kind'] == 'quest' and session['creator_id'] == user_id:
            return _out('forbidden', 'session', session)
        if not isinstance(option_key, str) or option_key not in _options(session):
            return _out('invalid', 'session', session)
        if session['kind'] == 'quiz':
            quote = _quote(c, guild_id, session['payload']['quote_id'])
            if not quote or quote['status'] != 'approved':
                return _out('closed', 'session', session)
        c.execute('SELECT option_key FROM community_votes WHERE session_id=? AND user_id=?', (session_id, user_id))
        previous = c.fetchone()
        if previous and previous[0] == option_key:
            return _out('unchanged', 'session', session)
        c.execute('INSERT INTO community_votes (session_id,user_id,option_key,updated_at) VALUES (?,?,?,?) ON CONFLICT(session_id,user_id) DO UPDATE SET option_key=excluded.option_key,updated_at=excluded.updated_at', (session_id, user_id, option_key, now))
        return _out('changed', 'session', session)


def _tally(c, session):
    counts = dict.fromkeys(_options(session), 0)
    c.execute('SELECT option_key,COUNT(*) FROM community_votes WHERE session_id=? GROUP BY option_key', (session['id'],))
    counts.update(dict(c.fetchall()))
    return counts


def session_tally(guild_id, session_id):
    with _connect() as c:
        session = _session(c, guild_id, session_id)
        return _tally(c, session) if session else None


def claim_session_close(guild_id, session_id, fallback, now=None):
    fallback = _text(fallback, 1900)
    with _connect(commit=True) as c:
        c.execute('BEGIN IMMEDIATE')
        now = _now(now)
        session = _session(c, guild_id, session_id)
        if not session:
            return _out('missing', 'session')
        if session['phase'] not in ACTIVE_PHASES:
            return _out('closed', 'session', session)
        if now < session['deadline']:
            return _out('not_due', 'session', session)
        if session['kind'] == 'quiz':
            quote = _quote(c, guild_id, session['payload'].get('quote_id', 1))
            if not quote or quote['status'] != 'approved':
                c.execute("UPDATE community_sessions SET phase='cancelled',payload='{}',result_text=NULL,closed_at=?,error_code='quote_removed' WHERE guild_id=? AND id=?", (now, guild_id, session_id))
                return _out('closed', 'session', _session(c, guild_id, session_id))
        tally = _tally(c, session)
        phase = 'expired' if session['phase'] == 'pending' or (session['kind'] == 'quest' and session['phase'] == 'active') else 'closing'
        title_awarded = False
        if session['kind'] == 'quest' and session['phase'] == 'verification' and tally['success'] > tally['not_yet']:
            c.execute('INSERT OR IGNORE INTO community_titles (guild_id,user_id,title_key,awarded_at,session_id) VALUES (?,?,?,?,?)', (guild_id, session['creator_id'], session['payload']['title_key'], now, session_id))
            title_awarded = c.rowcount == 1
        c.execute('UPDATE community_sessions SET phase=?,closed_at=?,result_text=?,generation_started_at=? WHERE guild_id=? AND id=?', (phase, now, fallback, now, guild_id, session_id))
        return _out('claimed', 'session', _session(c, guild_id, session_id), tally=tally, title_awarded=title_awarded)


def save_session_result(guild_id, session_id, result_text):
    result_text = _text(result_text, 1900)
    with _connect(commit=True) as c:
        c.execute('BEGIN IMMEDIATE')
        session = _session(c, guild_id, session_id)
        if not session:
            return _out('missing', 'session')
        if session['phase'] != 'closing':
            return _out('closed', 'session', session)
        if session['kind'] == 'quiz':
            quote = _quote(c, guild_id, session['payload'].get('quote_id', 1))
            if not quote or quote['status'] != 'approved':
                return _out('closed', 'session', session)
        c.execute("UPDATE community_sessions SET phase='closed',result_text=? WHERE guild_id=? AND id=? AND phase='closing'", (result_text, guild_id, session_id))
        return _out('saved', 'session', _session(c, guild_id, session_id))


def mark_session_delivery(guild_id, session_id, status, retry_at=None, error_code=None, expected_phase=None, expected_payload=None):
    if status not in ('pending', 'delivered', 'retry', 'error') or error_code not in (None, 'not_found', 'forbidden', 'http', 'database', 'cancelled', 'quote_removed'):
        raise ValueError('invalid_delivery')
    if retry_at is not None:
        retry_at = _now(retry_at)
    if status == 'retry' and retry_at is None:
        raise ValueError('invalid_retry')
    if expected_phase is not None and expected_phase not in ACTIVE_PHASES + TERMINAL_PHASES + ('closing',):
        raise ValueError('invalid_phase')
    if expected_payload is not None:
        if not isinstance(expected_payload, dict):
            raise ValueError('invalid_payload')
        try:
            expected_payload = json.dumps(expected_payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise ValueError('invalid_payload') from None
        if len(expected_payload) > 6000:
            raise ValueError('invalid_payload')
    with _connect(commit=True) as c:
        c.execute('BEGIN IMMEDIATE')
        session = _session(c, guild_id, session_id)
        if not session:
            return _out('missing', 'session')
        if expected_phase is not None and session['phase'] != expected_phase:
            return _out('stale', 'session', session)
        if expected_payload is not None and json.dumps(session['payload'], ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')) != expected_payload:
            return _out('stale', 'session', session)
        c.execute('UPDATE community_sessions SET delivery_status=?,retry_at=?,error_code=? WHERE guild_id=? AND id=?', (status, retry_at, error_code, _id(guild_id), _id(session_id)))
        return _out('changed' if c.rowcount else 'missing', 'session', _session(c, guild_id, session_id))


def cancel_session(guild_id, session_id, now=None):
    now = _now(now)
    with _connect(commit=True) as c:
        c.execute('BEGIN IMMEDIATE')
        session = _session(c, guild_id, session_id)
        if not session:
            return _out('missing', 'session')
        if session['phase'] not in ACTIVE_PHASES:
            return _out('closed', 'session', session)
        c.execute("UPDATE community_sessions SET phase='cancelled',closed_at=?,result_text=NULL,delivery_status='pending',retry_at=NULL,error_code='cancelled' WHERE guild_id=? AND id=?", (now, guild_id, session_id))
        return _out('cancelled', 'session', _session(c, guild_id, session_id))


def _week(value=None):
    if value is None:
        year, week, _ = datetime.date.today().isocalendar()
        return f'{year:04d}-W{week:02d}'
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-W\d{2}', value):
        raise ValueError('invalid_week')
    try:
        datetime.date.fromisocalendar(int(value[:4]), int(value[6:]), 1)
    except ValueError:
        raise ValueError('invalid_week') from None
    return value


def _seed(c, guild_id):
    c.execute('SELECT 1 FROM bingo_habits WHERE guild_id=? LIMIT 1', (guild_id,))
    if c.fetchone() is None:
        c.executemany('INSERT INTO bingo_habits (guild_id,text_en,text_ro) VALUES (?,?,?)', ((guild_id, en, ro) for en, ro in HABITS))


def _habits(c, guild_id, active_only, limit):
    c.execute('SELECT * FROM bingo_habits WHERE guild_id=?' + (' AND active=1' if active_only else '') + ' ORDER BY id LIMIT ?', (guild_id, limit))
    return [_row(c, row) for row in c.fetchall()]


def list_habits(guild_id, active_only=True, limit=50):
    _id(guild_id)
    _limit(limit, 50)
    if not isinstance(active_only, bool):
        raise ValueError('invalid_active')
    with _connect(commit=True) as c:
        c.execute('BEGIN IMMEDIATE')
        _seed(c, guild_id)
        return _habits(c, guild_id, active_only, limit)


def save_habit(guild_id, text_en, text_ro, habit_id=None):
    _id(guild_id)
    text_en, text_ro = _text(text_en, 200), _text(text_ro, 200)
    with _connect(commit=True) as c:
        c.execute('BEGIN IMMEDIATE')
        _seed(c, guild_id)
        if habit_id is None:
            c.execute('SELECT COUNT(*) FROM bingo_habits WHERE guild_id=? AND active=1', (guild_id,))
            if c.fetchone()[0] >= 50:
                return _out('limit', 'habit')
            c.execute('INSERT INTO bingo_habits (guild_id,text_en,text_ro) VALUES (?,?,?)', (guild_id, text_en, text_ro))
            habit_id = c.lastrowid
        else:
            c.execute('SELECT active FROM bingo_habits WHERE guild_id=? AND id=?', (guild_id, _id(habit_id)))
            existing = c.fetchone()
            if not existing:
                return _out('missing', 'habit')
            if not existing[0]:
                return _out('closed', 'habit')
            c.execute('UPDATE bingo_habits SET text_en=?,text_ro=? WHERE guild_id=? AND id=? AND active=1', (text_en, text_ro, guild_id, habit_id))
        c.execute('SELECT * FROM bingo_habits WHERE guild_id=? AND id=?', (guild_id, habit_id))
        return _out('saved', 'habit', _row(c, c.fetchone()))


def delete_habit(guild_id, habit_id):
    _id(guild_id)
    _id(habit_id)
    with _connect(commit=True) as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('SELECT * FROM bingo_habits WHERE guild_id=? AND id=? AND active=1', (guild_id, habit_id))
        habit = _row(c, c.fetchone())
        if not habit:
            return _out('missing', 'habit')
        c.execute('SELECT COUNT(*) FROM bingo_habits WHERE guild_id=? AND active=1', (guild_id,))
        if c.fetchone()[0] <= 9:
            return _out('minimum', 'habit', habit)
        c.execute('UPDATE bingo_habits SET active=0 WHERE guild_id=? AND id=?', (guild_id, habit_id))
        habit['active'] = 0
        return _out('deleted', 'habit', habit)


def _board(c, guild_id, board_id):
    c.execute('SELECT * FROM bingo_boards WHERE guild_id=? AND id=?', (_id(guild_id), _id(board_id)))
    board = _row(c, c.fetchone())
    if board:
        c.execute('SELECT * FROM bingo_marks WHERE board_id=? ORDER BY cell_index', (board_id,))
        board['marks'] = [_row(c, row) for row in c.fetchall()]
    return board


def get_or_create_board(guild_id, week_key=None, now=None):
    _id(guild_id)
    week_key = _week(week_key)
    now = _now(now)
    with _connect(commit=True) as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('SELECT id FROM bingo_boards WHERE guild_id=? AND week_key=?', (guild_id, week_key))
        existing = c.fetchone()
        if existing:
            return _board(c, guild_id, existing[0])
        _seed(c, guild_id)
        habits = _habits(c, guild_id, True, 50)
        if len(habits) < 9:
            raise ValueError('minimum_habits')
        seed = hashlib.sha256(f'{guild_id}:{week_key}'.encode()).digest()
        cells = random.Random(seed).sample(habits, 9)
        c.execute('INSERT INTO bingo_boards (guild_id,week_key,cells,created_at) VALUES (?,?,?,?)', (guild_id, week_key, json.dumps(cells, ensure_ascii=False, separators=(',', ':')), now))
        return _board(c, guild_id, c.lastrowid)


def get_board(guild_id, board_id):
    with _connect() as c:
        return _board(c, guild_id, board_id)


def _cell(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 8:
        raise ValueError('invalid_cell')
    return value


def mark_bingo_cell(guild_id, board_id, cell_index, user_id, now=None):
    _cell(cell_index)
    _id(user_id)
    now = _now(now)
    with _connect(commit=True) as c:
        c.execute('BEGIN IMMEDIATE')
        board = _board(c, guild_id, board_id)
        if not board:
            return _out('missing', 'board')
        if board['week_key'] != _week():
            return _out('closed', 'board', board)
        c.execute('INSERT OR IGNORE INTO bingo_marks (board_id,cell_index,marker_id,marked_at) VALUES (?,?,?,?)', (board_id, cell_index, user_id, now))
        return _out('marked' if c.rowcount else 'unchanged', 'board', _board(c, guild_id, board_id))


def confirm_bingo_cell(guild_id, board_id, cell_index, user_id, now=None):
    _cell(cell_index)
    _id(user_id)
    now = _now(now)
    with _connect(commit=True) as c:
        c.execute('BEGIN IMMEDIATE')
        board = _board(c, guild_id, board_id)
        if not board:
            return _out('missing', 'board')
        if board['week_key'] != _week():
            return _out('closed', 'board', board)
        mark = next((mark for mark in board['marks'] if mark['cell_index'] == cell_index), None)
        if not mark:
            return _out('unmarked', 'board', board)
        if mark['marker_id'] == user_id:
            return _out('forbidden', 'board', board)
        if mark['confirmer_id'] is not None:
            return _out('unchanged', 'board', board)
        c.execute('UPDATE bingo_marks SET confirmer_id=?,confirmed_at=? WHERE board_id=? AND cell_index=? AND confirmer_id IS NULL AND marker_id!=?', (user_id, now, board_id, cell_index, user_id))
        return _out('confirmed', 'board', _board(c, guild_id, board_id))


def create_quote_request(guild_id, source_channel_id, source_message_id, nominator_id, author_id, quote_text, author_name, language, now=None):
    for value in (guild_id, source_channel_id, source_message_id, nominator_id, author_id):
        _id(value)
    _text(quote_text, 1200)
    author_name = _text(author_name, 100)
    _language(language)
    with _connect(commit=True) as c:
        c.execute('BEGIN IMMEDIATE')
        now = _now(now)
        c.execute("DELETE FROM museum_quotes WHERE guild_id=? AND source_message_id=? AND status='pending' AND consent_expires_at<=?", (guild_id, source_message_id, now))
        c.execute('SELECT * FROM museum_quotes WHERE guild_id=? AND source_message_id=?', (guild_id, source_message_id))
        quote = _row(c, c.fetchone())
        if quote:
            quote['created'] = False
            return quote
        c.execute("INSERT INTO museum_quotes (guild_id,source_channel_id,source_message_id,nominator_id,author_id,quote_text,author_name,language,status,created_at,consent_expires_at) VALUES (?,?,?,?,?,?,?,?,'pending',?,?)", (guild_id, source_channel_id, source_message_id, nominator_id, author_id, quote_text, author_name, language, now, now + 86400))
        quote = _quote(c, guild_id, c.lastrowid)
        quote['created'] = True
        return quote


def bind_quote_consent_message(guild_id, quote_id, message_id, channel_id):
    _id(message_id)
    _id(channel_id)
    with _connect(commit=True) as c:
        c.execute('UPDATE museum_quotes SET consent_message_id=?,consent_channel_id=? WHERE guild_id=? AND id=?', (message_id, channel_id, _id(guild_id), _id(quote_id)))
        return _out('bound' if c.rowcount else 'missing', 'quote', _quote(c, guild_id, quote_id))


def get_quote(guild_id, quote_id, approved_only=False, now=None):
    now = _now(now)
    with _connect(commit=True) as c:
        quote = _quote(c, guild_id, quote_id)
        if quote and quote['status'] == 'pending' and quote['consent_expires_at'] <= now:
            c.execute("DELETE FROM museum_quotes WHERE guild_id=? AND id=? AND status='pending' AND consent_expires_at<=?", (guild_id, quote_id, now))
            return None
        if quote and approved_only and quote['status'] != 'approved':
            return None
        return quote


def list_pending_quotes(guild_id=None, limit=100, now=None, after_id=0):
    _limit(limit)
    now = _now(now)
    if isinstance(after_id, bool) or not isinstance(after_id, int) or after_id < 0:
        raise ValueError('invalid_cursor')
    clause = '' if guild_id is None else ' AND guild_id=?'
    params = [] if guild_id is None else [_id(guild_id)]
    with _connect(commit=True) as c:
        c.execute("DELETE FROM museum_quotes WHERE status='pending' AND consent_expires_at<=?" + clause, (now, *params))
        c.execute("SELECT * FROM museum_quotes WHERE status='pending' AND consent_expires_at>? AND id>?" + clause + ' ORDER BY id LIMIT ?', (now, after_id, *params, limit))
        return [_row(c, row) for row in c.fetchall()]


def approve_quote(guild_id, quote_id, author_id, now=None):
    _id(author_id)
    with _connect(commit=True) as c:
        c.execute('BEGIN IMMEDIATE')
        now = _now(now)
        quote = _quote(c, guild_id, quote_id)
        if not quote:
            return _out('missing', 'quote')
        if quote['author_id'] != author_id:
            return _out('forbidden', 'quote', quote)
        if quote['status'] != 'pending':
            return _out('unchanged', 'quote', quote)
        if now >= quote['consent_expires_at']:
            c.execute("DELETE FROM museum_quotes WHERE guild_id=? AND id=? AND status='pending'", (guild_id, quote_id))
            return _out('expired', 'quote')
        c.execute("UPDATE museum_quotes SET status='approved',approved_at=? WHERE guild_id=? AND id=? AND status='pending' AND consent_expires_at>?", (now, guild_id, quote_id, now))
        return _out('approved', 'quote', _quote(c, guild_id, quote_id))


def delete_quote(guild_id, quote_id, author_id=None, now=None):
    if author_id is not None:
        _id(author_id)
    now = _now(now)
    with _connect(commit=True) as c:
        c.execute('BEGIN IMMEDIATE')
        quote = _quote(c, guild_id, quote_id)
        if not quote:
            return _out('missing', 'quote')
        if author_id is not None and quote['author_id'] != author_id:
            return _out('forbidden', 'quote')
        c.execute("SELECT * FROM community_sessions WHERE guild_id=? AND kind='quiz'", (guild_id,))
        sessions = [_row(c, row) for row in c.fetchall()]
        for session in sessions:
            if session['payload'].get('quote_id') != quote_id:
                continue
            phase = 'cancelled' if session['phase'] in ACTIVE_PHASES + ('closing',) else session['phase']
            c.execute("UPDATE community_sessions SET phase=?,payload='{}',result_text=NULL,closed_at=COALESCE(closed_at,?),delivery_status='pending',retry_at=NULL,error_code='quote_removed' WHERE guild_id=? AND id=?", (phase, now, guild_id, session['id']))
            c.execute('DELETE FROM community_votes WHERE session_id=?', (session['id'],))
        c.execute('DELETE FROM museum_quotes WHERE guild_id=? AND id=?', (guild_id, quote_id))
        return _out('deleted', 'quote')


def list_approved_quotes(guild_id, limit=100, after_id=None):
    _id(guild_id)
    _limit(limit)
    if after_id is not None and (isinstance(after_id, bool) or not isinstance(after_id, int) or after_id < 0):
        raise ValueError('invalid_cursor')
    with _connect() as c:
        if after_id is None:
            c.execute("SELECT * FROM museum_quotes WHERE guild_id=? AND status='approved' ORDER BY id DESC LIMIT ?", (guild_id, limit))
        else:
            c.execute("SELECT * FROM museum_quotes WHERE guild_id=? AND status='approved' AND id>? ORDER BY id LIMIT ?", (guild_id, after_id, limit))
        return [_row(c, row) for row in c.fetchall()]


def list_quote_guilds(limit=100, after_guild_id=0):
    _limit(limit)
    if isinstance(after_guild_id, bool) or not isinstance(after_guild_id, int) or not 0 <= after_guild_id <= 9223372036854775807:
        raise ValueError('invalid_cursor')
    with _connect() as c:
        c.execute("SELECT DISTINCT guild_id FROM museum_quotes WHERE status='approved' AND guild_id>? ORDER BY guild_id LIMIT ?", (after_guild_id, limit))
        return [_row(c, row) for row in c.fetchall()]


def choose_quote(guild_id):
    with _connect() as c:
        c.execute("SELECT * FROM museum_quotes WHERE guild_id=? AND status='approved' ORDER BY RANDOM() LIMIT 1", (_id(guild_id),))
        return _row(c, c.fetchone())


def list_titles(guild_id, user_id=None, limit=100):
    _id(guild_id)
    _limit(limit)
    clause, params = ('', []) if user_id is None else (' AND user_id=?', [_id(user_id)])
    with _connect() as c:
        c.execute('SELECT * FROM community_titles WHERE guild_id=?' + clause + ' ORDER BY awarded_at DESC,title_key LIMIT ?', (guild_id, *params, limit))
        return [_row(c, row) for row in c.fetchall()]


def delete_title(guild_id, user_id, title_key):
    _text(title_key, 80)
    with _connect(commit=True) as c:
        c.execute('DELETE FROM community_titles WHERE guild_id=? AND user_id=? AND title_key=?', (_id(guild_id), _id(user_id), title_key))
        return {'status': 'deleted' if c.rowcount else 'missing'}


def cleanup_community(now=None):
    now = _now(now)
    cutoff = now - 30 * 86400
    with _connect(commit=True) as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("DELETE FROM museum_quotes WHERE status='pending' AND consent_expires_at<=?", (now,))
        quotes = c.rowcount
        c.execute("DELETE FROM community_sessions WHERE phase IN ('closed','cancelled','declined','expired') AND closed_at<? AND delivery_status IN ('delivered','error')", (cutoff,))
        sessions = c.rowcount
        c.execute('SELECT DISTINCT week_key FROM bingo_boards WHERE week_key<?', (_week(),))
        weeks = [row[0] for row in c.fetchall()]
        boards = 0
        for week in weeks:
            monday = datetime.date.fromisocalendar(int(week[:4]), int(week[6:]), 1)
            week_end = datetime.datetime.combine(monday + datetime.timedelta(days=7), datetime.time()).timestamp()
            if week_end < cutoff:
                c.execute('DELETE FROM bingo_boards WHERE week_key=?', (week,))
                boards += c.rowcount
        return {'quotes': quotes, 'sessions': sessions, 'boards': boards}
