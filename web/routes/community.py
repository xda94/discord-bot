import re
import time
from functools import wraps

from flask import Blueprint, jsonify, request

from db import community
from db.connection import _connect
from web.auth import require_token
from web.helpers import discord_id, is_int

blueprint = Blueprint('community', __name__)


def _positive_id(value, name):
    parsed = discord_id(value, name)
    if parsed <= 0:
        raise ValueError('A positive ID is required')
    return parsed


def _body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError('A JSON object is required')
    return data


def _guild(data=None):
    return _positive_id((request.args if data is None else data).get('guild_id'), 'guild_id')


def _limit():
    raw = request.args.get('limit', '100')
    if not raw.isascii() or not raw.isdigit() or not 1 <= int(raw) <= 100:
        raise ValueError('Limit must be between 1 and 100')
    return int(raw)


def _record_id(value):
    if not is_int(value) or not 0 < value <= 9223372036854775807:
        raise ValueError('A positive record ID is required')
    return value


def _habit_text(data, name):
    value = data.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise ValueError('Habit translations must contain 1 to 200 characters')
    return value.strip()


def _safe(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ValueError:
            return jsonify(error='Invalid community request'), 400
        except Exception:
            return jsonify(error='Community data is temporarily unavailable. Please try again.'), 503
    return wrapped


def _outcome(result, accepted):
    if result['status'] == 'missing':
        return jsonify(error='Community record not found'), 404
    if result['status'] not in accepted:
        return jsonify(error='This community change is not allowed'), 409
    return jsonify(result)


@blueprint.get('/community/sessions')
@require_token
@_safe
def sessions():
    guild_id = _guild()
    rows = community.list_sessions(guild_id, limit=_limit())
    fields = ('id', 'guild_id', 'channel_id', 'creator_id', 'subject_id', 'kind', 'phase', 'language', 'payload', 'message_id', 'created_at', 'deadline', 'closed_at', 'result_text', 'delivery_status', 'retry_at', 'error_code')
    items = []
    for row in rows:
        item = {key: row[key] for key in fields}
        if row['kind'] == 'quiz':
            quote_id = row['payload'].get('quote_id')
            if quote_id is None or community.get_quote(guild_id, quote_id, approved_only=True) is None:
                item['payload'] = {}
                item['result_text'] = None
        item['tally'] = community.session_tally(guild_id, row['id']) or {}
        items.append(item)
    return jsonify(guild_id=guild_id, sessions=items)


@blueprint.post('/community/sessions/<int:session_id>/cancel')
@require_token
@_safe
def cancel_session(session_id):
    return _outcome(community.cancel_session(_guild(_body()), _record_id(session_id)), {'cancelled'})


@blueprint.get('/community/bingo')
@require_token
@_safe
def bingo():
    guild_id = _guild()
    return jsonify(guild_id=guild_id, board=community.get_or_create_board(guild_id), habits=community.list_habits(guild_id))


@blueprint.post('/community/bingo/habits')
@blueprint.put('/community/bingo/habits/<int:habit_id>')
@require_token
@_safe
def save_habit(habit_id=None):
    data = _body()
    if habit_id is not None:
        _record_id(habit_id)
    result = community.save_habit(_guild(data), _habit_text(data, 'text_en'), _habit_text(data, 'text_ro'), habit_id=habit_id)
    return _outcome(result, {'saved'})


@blueprint.delete('/community/bingo/habits/<int:habit_id>')
@require_token
@_safe
def delete_habit(habit_id):
    return _outcome(community.delete_habit(_guild(_body()), _record_id(habit_id)), {'deleted'})


@blueprint.get('/community/quotes')
@require_token
@_safe
def quotes():
    guild_id = _guild()
    rows = community.list_approved_quotes(guild_id, limit=_limit())
    fields = ('id', 'guild_id', 'source_channel_id', 'source_message_id', 'nominator_id', 'author_id', 'quote_text', 'author_name', 'language', 'status', 'created_at', 'approved_at')
    with _connect() as cursor:
        cursor.execute("SELECT COUNT(*) FROM museum_quotes WHERE guild_id=? AND status='pending' AND consent_expires_at>?", (guild_id, time.time()))
        pending_count = cursor.fetchone()[0]
    return jsonify(guild_id=guild_id, quotes=[{key: row[key] for key in fields} for row in rows], pending_count=pending_count)


@blueprint.delete('/community/quotes/<int:quote_id>')
@require_token
@_safe
def delete_quote(quote_id):
    return _outcome(community.delete_quote(_guild(_body()), _record_id(quote_id)), {'deleted'})


@blueprint.get('/community/titles')
@require_token
@_safe
def titles():
    guild_id = _guild()
    return jsonify(guild_id=guild_id, titles=community.list_titles(guild_id, limit=_limit()))


@blueprint.delete('/community/titles/<user_id>/<title_key>')
@require_token
@_safe
def delete_title(user_id, title_key):
    if not re.fullmatch(r'[a-z0-9_-]{1,80}', title_key):
        raise ValueError('Invalid title key')
    return _outcome(community.delete_title(_guild(_body()), _positive_id(user_id, 'user_id'), title_key), {'deleted'})
