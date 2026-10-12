import concurrent.futures
import datetime
import threading
from contextlib import contextmanager

import pytest

import db
from db import community
from db.connection import _connect

QUEST = {
    'challenge_en': 'Share a harmless pun',
    'challenge_ro': 'Spune un joc de cuvinte',
    'title_key': 'pun_master',
    'title_en': 'Pun master',
    'title_ro': 'Maestrul jocurilor de cuvinte',
}


def meeting(now=100):
    session = community.create_session(1, 10, 20, 'sedinta', 'en', {'subject': 'Snack', 'options': ['Tea', 'Coffee']}, now=now)
    community.bind_session_message(1, session['id'], 1000)
    return community.get_session(1, session['id'])


def quote(now=100):
    return community.create_quote_request(1, 10, 1000, 20, 30, 'A harmless quote', 'Ada', 'en', now=now)


def quiz(q, now=100):
    return community.create_session(1, 10, 20, 'quiz', 'en', {
        'quote_id': q['id'], 'quote_text': q['quote_text'],
        'candidates': [{'user_id': 30, 'name': 'Ada'}, {'user_id': 40, 'name': 'Bob'}],
        'correct_author_id': 30,
    }, now=now)


def current_week():
    year, week, _ = datetime.date.today().isocalendar()
    return f'{year}-W{week:02d}'


def mutation_after_lock_wait(monkeypatch, deadline, mutation):
    clock = [deadline - 1]
    attempted = threading.Event()

    @contextmanager
    def observed_connect(commit=False):
        with _connect(commit=commit) as c:
            c.connection.set_trace_callback(lambda sql: attempted.set() if sql == 'BEGIN IMMEDIATE' else None)
            yield c

    monkeypatch.setattr(community, '_connect', observed_connect)
    monkeypatch.setattr(community.time, 'time', lambda: clock[0])
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        with _connect(commit=True) as c:
            c.execute('BEGIN IMMEDIATE')
            result = pool.submit(mutation)
            assert attempted.wait(timeout=2)
            clock[0] = deadline
        return result.result(timeout=3)


def test_migration_idempotent_and_preserves_other_rows(tmp_db):
    with _connect(commit=True) as c:
        c.execute("INSERT INTO responses (guild_id,keyword,response) VALUES (1,'hello','there')")
    session = meeting()
    db.init_db()
    db.init_db()
    assert community.get_session(1, session['id']) == session
    with _connect() as c:
        assert c.execute('SELECT keyword,response FROM responses').fetchall() == [('hello', 'there')]


def test_cross_guild_session_reads_and_mutations(tmp_db):
    session = meeting()
    sid = session['id']
    assert community.get_session(2, sid) is None
    assert community.session_tally(2, sid) is None
    assert community.bind_session_message(2, sid, 55)['status'] == 'missing'
    assert community.cast_vote(2, sid, 50, '0', now=200)['status'] == 'missing'
    assert community.transition_session(2, sid, 'active', 'closed', 20, now=200)['status'] == 'missing'
    assert community.claim_session_close(2, sid, 'Fallback', now=701)['status'] == 'missing'
    assert community.save_session_result(2, sid, 'Result')['status'] == 'missing'
    assert community.mark_session_delivery(2, sid, 'delivered')['status'] == 'missing'
    assert community.cancel_session(2, sid)['status'] == 'missing'
    assert community.get_session(1, sid) == session


def test_votes_change_are_bounded_and_check_deadline(tmp_db):
    session = meeting()
    sid = session['id']
    assert community.cast_vote(1, sid, 30, '0', now=100)['status'] == 'changed'
    assert community.cast_vote(1, sid, 30, '0', now=200)['status'] == 'unchanged'
    assert community.cast_vote(1, sid, 30, '1', now=200)['status'] == 'changed'
    assert community.cast_vote(1, sid, 31, 'invalid', now=200)['status'] == 'invalid'
    assert community.cast_vote(1, sid, 31, '0', now=700)['status'] == 'closed'
    assert community.session_tally(1, sid) == {'0': 0, '1': 1}
    with _connect() as c:
        assert c.execute('SELECT COUNT(*) FROM community_votes').fetchone()[0] == 1


def test_public_vote_requires_message_binding(tmp_db):
    session = community.create_session(1, 10, 20, 'sedinta', 'en', {'subject': 'Snack', 'options': ['Tea', 'Coffee']}, now=100)
    assert community.cast_vote(1, session['id'], 30, '0', now=101)['status'] == 'closed'


def test_tribunal_immediate_and_exact_vote_window(tmp_db):
    session = community.create_session(1, 10, 20, 'tribunal', 'ro', {'accusation': 'Forgot coffee'}, subject_id=30, now=100)
    sid = session['id']
    community.bind_session_message(1, sid, 1000)
    assert session['phase'] == 'active' and session['deadline'] == 700
    for user_id in (20, 30, 40):
        assert community.cast_vote(1, sid, user_id, 'guilty', now=150)['status'] == 'changed'
    assert community.cast_vote(1, sid, 40, 'innocent', now=699)['status'] == 'changed'
    assert community.cast_vote(1, sid, 41, 'innocent', now=700)['status'] == 'closed'
    assert community.transition_session(1, sid, 'pending', 'active', 30, now=201)['status'] == 'closed'


@pytest.mark.parametrize('operation', ['vote', 'tribunal', 'quest_accept', 'quest_complete', 'quote', 'close'])
def test_default_mutation_clock_is_sampled_after_write_lock(tmp_db, monkeypatch, operation):
    if operation == 'quote':
        q = quote()
        result = mutation_after_lock_wait(monkeypatch, q['consent_expires_at'], lambda: community.approve_quote(1, q['id'], 30))
        assert result['status'] == 'expired'
        assert community.get_quote(1, q['id']) is None
        return
    if operation in ('vote', 'close'):
        session = meeting()
    elif operation == 'tribunal':
        session = community.create_session(1, 10, 20, 'tribunal', 'en', {'accusation': 'Forgot coffee'}, subject_id=30, now=100)
        community.bind_session_message(1, session['id'], 1000)
    else:
        session = community.create_session(1, 10, 20, 'quest', 'en', QUEST, now=100)
        community.bind_session_message(1, session['id'], 1000)
        if operation == 'quest_complete':
            community.transition_session(1, session['id'], 'pending', 'active', 20, now=200)
            session = community.get_session(1, session['id'])
    sid = session['id']
    if operation == 'vote':
        mutation = lambda: community.cast_vote(1, sid, 30, '0')
    elif operation == 'close':
        mutation = lambda: community.claim_session_close(1, sid, 'Fallback')
    else:
        phase, new_phase = ('active', 'verification') if operation == 'quest_complete' else ('pending', 'active')
        actor = 30 if operation == 'tribunal' else 20
        mutation = lambda: community.transition_session(1, sid, phase, new_phase, actor)
    result = mutation_after_lock_wait(monkeypatch, session['deadline'], mutation)
    assert result['status'] == ('claimed' if operation == 'close' else 'closed')
    if operation == 'close':
        assert result['session']['generation_started_at'] == session['deadline']
    else:
        assert community.get_session(1, sid)['phase'] == session['phase']
        if operation == 'vote':
            assert community.session_tally(1, sid) == {'0': 0, '1': 0}


@pytest.mark.parametrize('kind,duration', [('sedinta', 600), ('quote', 86400)])
def test_creation_window_starts_after_write_lock(tmp_db, monkeypatch, kind, duration):
    if kind == 'quote':
        mutation = lambda: community.create_quote_request(1, 10, 1000, 20, 30, 'Stored quote', 'Ada', 'en')
    else:
        mutation = lambda: community.create_session(1, 10, 20, kind, 'en', {'subject': 'Snack', 'options': ['Tea', 'Coffee']})
    result = mutation_after_lock_wait(monkeypatch, 1000, mutation)
    assert result['created_at'] == 1000
    deadline_key = 'consent_expires_at' if kind == 'quote' else 'deadline'
    assert result[deadline_key] == 1000 + duration


def test_concurrent_close_claim_has_one_winner_and_persisted_fallback(tmp_db):
    session = meeting()
    sid = session['id']
    community.cast_vote(1, sid, 30, '1', now=200)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: community.claim_session_close(1, sid, 'Fallback', now=700), range(4)))
    assert [result['status'] for result in results].count('claimed') == 1
    winner = next(result for result in results if result['status'] == 'claimed')
    assert winner['tally'] == {'0': 0, '1': 1}
    stored = community.get_session(1, sid)
    assert stored['phase'] == 'closing'
    assert stored['generation_started_at'] == 700
    assert stored['result_text'] == 'Fallback'
    assert community.cast_vote(1, sid, 31, '0', now=600)['status'] == 'closed'
    assert community.save_session_result(1, sid, stored['result_text'])['status'] == 'saved'
    assert community.save_session_result(1, sid, 'Second result')['status'] == 'closed'
    assert community.get_session(1, sid)['result_text'] == 'Fallback'


def test_quest_reuse_self_verification_and_atomic_title_award(tmp_db):
    session = community.create_session(1, 10, 20, 'quest', 'en', QUEST, now=100)
    sid = session['id']
    assert community.create_session(1, 11, 20, 'quest', 'ro', QUEST, now=101)['id'] == sid
    assert not community.create_session(1, 11, 20, 'quest', 'ro', QUEST, now=101)['created']
    community.bind_session_message(1, sid, 1000)
    active = community.transition_session(1, sid, 'pending', 'active', 20, now=200)
    assert active['session']['deadline'] == 200 + 604800
    verifying = community.transition_session(1, sid, 'active', 'verification', 20, now=300)
    assert verifying['session']['deadline'] == 900
    assert community.cast_vote(1, sid, 20, 'success', now=400)['status'] == 'forbidden'
    assert community.cast_vote(1, sid, 30, 'success', now=400)['status'] == 'changed'
    closed = community.claim_session_close(1, sid, 'Success', now=900)
    assert closed['title_awarded']
    assert len(community.list_titles(1, 20)) == 1
    assert community.claim_session_close(1, sid, 'Success', now=901)['status'] == 'closed'
    assert len(community.list_titles(1, 20)) == 1
    assert community.list_titles(2) == []
    assert community.delete_title(2, 20, 'pun_master')['status'] == 'missing'
    assert community.delete_title(1, 20, 'pun_master')['status'] == 'deleted'


@pytest.mark.parametrize('votes', [[], ['success', 'not_yet'], ['not_yet']])
def test_quest_neutral_or_negative_never_awards(tmp_db, votes):
    session = community.create_session(1, 10, 20, 'quest', 'en', QUEST, now=100)
    sid = session['id']
    community.bind_session_message(1, sid, 1000)
    community.transition_session(1, sid, 'pending', 'active', 20, now=200)
    community.transition_session(1, sid, 'active', 'verification', 20, now=300)
    for user, option in enumerate(votes, 30):
        community.cast_vote(1, sid, user, option, now=400)
    assert not community.claim_session_close(1, sid, 'Finished', now=900)['title_awarded']
    assert community.list_titles(1) == []


def test_bingo_snapshots_cross_guild_and_independent_confirmation(tmp_db):
    board = community.get_or_create_board(1)
    bid = board['id']
    assert board['week_key'] == current_week()
    assert len(board['cells']) == 9
    assert all(cell['text_en'] and cell['text_ro'] for cell in board['cells'])
    assert community.get_or_create_board(1)['id'] == bid
    cell = board['cells'][0]
    community.save_habit(1, 'Changed habit', 'Obicei schimbat', habit_id=cell['id'])
    assert community.get_board(1, bid)['cells'] == board['cells']
    assert community.get_board(2, bid) is None
    assert community.mark_bingo_cell(2, bid, 0, 30)['status'] == 'missing'
    assert community.confirm_bingo_cell(2, bid, 0, 40)['status'] == 'missing'
    assert community.mark_bingo_cell(1, bid, 0, 30)['status'] == 'marked'
    assert community.mark_bingo_cell(1, bid, 0, 40)['status'] == 'unchanged'
    assert community.confirm_bingo_cell(1, bid, 0, 30)['status'] == 'forbidden'
    assert community.confirm_bingo_cell(1, bid, 0, 40)['status'] == 'confirmed'
    assert community.confirm_bingo_cell(1, bid, 0, 50)['status'] == 'unchanged'
    assert community.get_board(1, bid)['marks'][0]['confirmer_id'] == 40
    old = community.get_or_create_board(1, '2020-W01')
    assert community.mark_bingo_cell(1, old['id'], 0, 30)['status'] == 'closed'


def test_habit_minimum_maximum_and_guild_scope(tmp_db):
    habits = community.list_habits(1)
    hid = habits[0]['id']
    assert community.save_habit(2, 'Hello', 'Salut', habit_id=hid)['status'] == 'missing'
    assert community.delete_habit(2, hid)['status'] == 'missing'
    for habit in habits[:3]:
        assert community.delete_habit(1, habit['id'])['status'] == 'deleted'
    assert community.delete_habit(1, habits[3]['id'])['status'] == 'minimum'
    for number in range(41):
        assert community.save_habit(1, f'Habit {number}', f'Obicei {number}')['status'] == 'saved'
    assert len(community.list_habits(1)) == 50
    assert community.save_habit(1, 'Too many', 'Prea multe')['status'] == 'limit'


def test_quote_consent_deadlines_and_guild_scope(tmp_db):
    q = quote()
    qid = q['id']
    assert community.get_quote(1, qid, approved_only=True, now=200) is None
    assert community.list_approved_quotes(1) == []
    assert community.choose_quote(1) is None
    assert community.get_quote(2, qid, now=200) is None
    assert community.bind_quote_consent_message(2, qid, 100, 10)['status'] == 'missing'
    assert community.approve_quote(2, qid, 30, now=200)['status'] == 'missing'
    assert community.delete_quote(2, qid)['status'] == 'missing'
    assert community.approve_quote(1, qid, 40, now=200)['status'] == 'forbidden'
    assert community.bind_quote_consent_message(1, qid, 100, 10)['status'] == 'bound'
    assert community.approve_quote(1, qid, 30, now=200)['status'] == 'approved'
    assert community.list_pending_quotes(1, now=300) == []
    assert community.choose_quote(1)['quote_text'] == q['quote_text']
    assert community.approve_quote(1, qid, 30, now=300)['status'] == 'unchanged'
    expired = community.create_quote_request(1, 10, 1001, 20, 30, 'Temporary text', 'Ada', 'en', now=100)
    assert community.approve_quote(1, expired['id'], 30, now=86500)['status'] == 'expired'
    with _connect() as c:
        assert c.execute('SELECT quote_text FROM museum_quotes WHERE id=?', (expired['id'],)).fetchone() is None


def test_quote_snapshot_preserves_exact_whitespace(tmp_db):
    snapshot = '  A quote with surrounding whitespace.\n'
    q = community.create_quote_request(1, 10, 1000, 20, 30, snapshot, 'Ada', 'en', now=100)
    assert q['quote_text'] == snapshot
    community.approve_quote(1, q['id'], 30, now=200)
    assert community.choose_quote(1)['quote_text'] == snapshot


def test_quote_guilds_include_all_approved_guilds_and_only_ids(tmp_db):
    quote()
    large_guild = 9223372036854775807
    approved = []
    for guild, message_id in ((50, 1000), (50, 1001), (large_guild, 1000)):
        q = community.create_quote_request(guild, 10, message_id, 20, 30, 'Stored quote', 'Ada', 'en', now=100)
        community.approve_quote(guild, q['id'], 30, now=200)
        approved.append(q)
    assert community.list_quote_guilds() == [{'guild_id': 50}, {'guild_id': large_guild}]
    assert community.list_quote_guilds(after_guild_id=50) == [{'guild_id': large_guild}]
    assert community.list_quote_guilds(after_guild_id=large_guild) == []
    assert len(community.list_approved_quotes(50)) == 2
    assert community.get_quote(large_guild, approved[-1]['id'], approved_only=True, now=300)['quote_text'] == 'Stored quote'
    assert community.get_quote(50, approved[-1]['id'], approved_only=True, now=300) is None
    community.delete_quote(large_guild, approved[-1]['id'])
    assert community.list_quote_guilds() == [{'guild_id': 50}]


def test_quote_guilds_paginate_beyond_one_hundred(tmp_db):
    for guild in range(101, 206):
        q = community.create_quote_request(guild, 10, 1000, 20, 30, 'Stored quote', 'Ada', 'en', now=100)
        community.approve_quote(guild, q['id'], 30, now=200)
    first = community.list_quote_guilds()
    second = community.list_quote_guilds(after_guild_id=first[-1]['guild_id'])
    assert len(first) == 100
    assert first + second == [{'guild_id': guild} for guild in range(101, 206)]
    assert community.list_quote_guilds(after_guild_id=second[-1]['guild_id']) == []
    assert community.list_quote_guilds(limit=1) == [{'guild_id': 101}]


@pytest.mark.parametrize('arguments', [
    {'limit': 0}, {'limit': 101}, {'limit': True}, {'limit': 1.0},
    {'after_guild_id': -1}, {'after_guild_id': 2 ** 63},
    {'after_guild_id': True}, {'after_guild_id': 1.0},
    {'after_guild_id': '1'}, {'after_guild_id': None},
])
def test_quote_guild_enumeration_validation(tmp_db, arguments):
    with pytest.raises(ValueError):
        community.list_quote_guilds(**arguments)


def test_quote_revocation_erases_open_and_closed_quiz_content(tmp_db):
    q = quote()
    with pytest.raises(ValueError, match='quote_unavailable'):
        quiz(q)
    community.approve_quote(1, q['id'], 30, now=200)
    open_quiz = quiz(q, now=300)
    closed_quiz = quiz(q, now=300)
    for session in (open_quiz, closed_quiz):
        community.bind_session_message(1, session['id'], 1000 + session['id'])
    community.claim_session_close(1, closed_quiz['id'], q['quote_text'], now=900)
    assert community.delete_quote(1, q['id'], author_id=40)['status'] == 'forbidden'
    assert community.delete_quote(1, q['id'], author_id=30)['status'] == 'deleted'
    for session in (open_quiz, closed_quiz):
        stored = community.get_session(1, session['id'])
        assert stored['phase'] == 'cancelled'
        assert stored['payload'] == {}
        assert stored['result_text'] is None
        assert community.save_session_result(1, session['id'], q['quote_text'])['status'] == 'closed'
        assert community.claim_session_close(1, session['id'], q['quote_text'], now=1000)['status'] == 'closed'
    with _connect() as c:
        assert c.execute('SELECT COUNT(*) FROM museum_quotes').fetchone()[0] == 0
        assert q['quote_text'] not in str(c.execute('SELECT payload,result_text FROM community_sessions').fetchall())


@pytest.mark.parametrize('closed', [False, True])
def test_delivery_acknowledgment_preserves_quote_revocation_reconciliation(tmp_db, closed):
    q = quote()
    community.approve_quote(1, q['id'], 30, now=200)
    session = quiz(q, now=300)
    sid = session['id']
    community.bind_session_message(1, sid, 1000)
    if closed:
        community.claim_session_close(1, sid, q['quote_text'], now=900)
        community.save_session_result(1, sid, q['quote_text'])
    rendered = community.get_session(1, sid)
    community.mark_session_delivery(1, sid, 'delivered')
    community.delete_quote(1, q['id'], now=1000)
    revoked = community.get_session(1, sid)
    assert revoked['delivery_status'] == 'pending'
    assert revoked['retry_at'] is None
    assert revoked['error_code'] == 'quote_removed'
    if closed:
        assert revoked['phase'] == rendered['phase'] == 'closed'
    else:
        assert revoked['phase'] == 'cancelled'
        assert community.mark_session_delivery(1, sid, 'delivered', expected_phase=rendered['phase'])['status'] == 'stale'
    assert community.mark_session_delivery(1, sid, 'delivered', expected_payload=rendered['payload'])['status'] == 'stale'
    result = community.mark_session_delivery(1, sid, 'delivered', expected_phase=rendered['phase'], expected_payload=rendered['payload'])
    assert result == {'status': 'stale', 'session': revoked}
    assert community.get_session(1, sid) == revoked
    reconciled = community.mark_session_delivery(1, sid, 'delivered', expected_phase=revoked['phase'], expected_payload={})
    assert reconciled['status'] == 'changed'
    assert reconciled['session']['delivery_status'] == 'delivered'


def test_delivery_guards_are_optional_exact_and_guild_scoped(tmp_db):
    session = meeting()
    sid = session['id']
    assert community.mark_session_delivery(2, sid, 'delivered', expected_phase=session['phase'], expected_payload=session['payload']) == {'status': 'missing', 'session': None}
    assert community.get_session(1, sid) == session
    changed = community.mark_session_delivery(1, sid, 'retry', retry_at=750, error_code='http')
    assert changed['status'] == 'changed'
    stale = community.mark_session_delivery(1, sid, 'delivered', expected_phase='closed', expected_payload=session['payload'])
    assert stale == {'status': 'stale', 'session': changed['session']}
    assert community.get_session(1, sid) == changed['session']
    assert community.mark_session_delivery(1, sid, 'delivered', expected_phase=session['phase'], expected_payload=dict(reversed(list(session['payload'].items()))))['status'] == 'changed'


@pytest.mark.parametrize('expectation', [
    {'expected_phase': 'invented'},
    {'expected_phase': 1},
    {'expected_payload': []},
    {'expected_payload': {'data': float('nan')}},
    {'expected_payload': {'data': float('inf')}},
    {'expected_payload': {'data': object()}},
    {'expected_payload': {'data': 'x' * 6000}},
])
def test_delivery_guard_validation_is_safe_and_nonmutating(tmp_db, expectation):
    session = meeting()
    with pytest.raises(ValueError):
        community.mark_session_delivery(1, session['id'], 'delivered', **expectation)
    assert community.get_session(1, session['id']) == session


def test_cancel_delivery_pagination_and_retention(tmp_db):
    now = 50 * 86400
    old = meeting(now=100)
    community.claim_session_close(1, old['id'], 'Done', now=700)
    community.save_session_result(1, old['id'], 'Done')
    community.mark_session_delivery(1, old['id'], 'delivered')
    active = meeting(now=now)
    overdue = meeting(now=100)
    pending_delivery = meeting(now=100)
    community.cancel_session(1, pending_delivery['id'], now=100)
    assert community.cast_vote(1, pending_delivery['id'], 50, '0', now=101)['status'] == 'closed'
    expired = quote()
    approved = community.create_quote_request(1, 10, 1001, 20, 30, 'Permanent text', 'Ada', 'en', now=100)
    community.approve_quote(1, approved['id'], 30, now=200)
    old_board = community.get_or_create_board(1, '1970-W01', now=100)
    board = community.get_or_create_board(1, now=100)
    result = community.cleanup_community(now=now)
    assert result == {'sessions': 1, 'quotes': 1, 'boards': 1}
    assert community.get_session(1, old['id']) is None
    assert community.get_session(1, active['id'])
    assert community.get_session(1, overdue['id'])
    assert community.get_session(1, pending_delivery['id'])
    assert community.get_quote(1, expired['id'], now=now) is None
    assert community.get_quote(1, approved['id'], now=now)
    assert community.get_board(1, old_board['id']) is None
    assert community.get_board(1, board['id'])
    first = community.list_due_sessions(now=now, limit=1)
    second = community.list_due_sessions(now=now, limit=1, after_id=first[0]['id'])
    assert first[0]['id'] != second[0]['id']


@pytest.mark.parametrize('boundary_offset,removed', [(-1, False), (0, False), (1, True)])
def test_bingo_retention_counts_from_local_week_end(tmp_db, boundary_offset, removed):
    monday = datetime.date.fromisocalendar(2020, 13, 1)
    monday_time = datetime.datetime.combine(monday, datetime.time()).timestamp()
    sunday_time = datetime.datetime.combine(monday + datetime.timedelta(days=6), datetime.time(23, 59)).timestamp()
    week_end = datetime.datetime.combine(monday + datetime.timedelta(days=7), datetime.time()).timestamp()
    monday_board = community.get_or_create_board(1, '2020-W13', now=monday_time)
    sunday_board = community.get_or_create_board(2, '2020-W13', now=sunday_time)
    current_board = community.get_or_create_board(1, now=monday_time)
    result = community.cleanup_community(now=week_end + 30 * 86400 + boundary_offset)
    assert result == {'quotes': 0, 'sessions': 0, 'boards': 2 if removed else 0}
    assert (community.get_board(1, monday_board['id']) is None) == removed
    assert (community.get_board(2, sunday_board['id']) is None) == removed
    assert community.get_board(1, current_board['id']) is not None


@pytest.mark.parametrize('call', [
    lambda: community.list_sessions(0),
    lambda: community.list_sessions(1, limit=101),
    lambda: community.list_habits(1, limit=51),
    lambda: community.list_approved_quotes(1, limit=0),
    lambda: community.get_session(True, 1),
    lambda: community.get_session(2 ** 63, 1),
    lambda: community.create_session(1, 10, 20, 'sedinta', 'en', {'subject': 'x', 'options': ['same', 'SAME']}),
    lambda: community.create_session(1, 10, 20, 'tribunal', 'en', {'accusation': 'x' * 501}, subject_id=30),
    lambda: community.create_session(1, 10, 20, 'quest', 'en', dict(QUEST, url='private')),
    lambda: community.mark_bingo_cell(1, 1, 9, 30),
    lambda: community.save_habit(1, 'x' * 201, 'x'),
    lambda: community.create_quote_request(1, 10, 100, 20, 30, 'x' * 1201, 'Ada', 'en'),
    lambda: community.mark_session_delivery(1, 1, 'error', error_code='raw_exception'),
    lambda: community.cleanup_community(float('nan')),
])
def test_input_validation(tmp_db, call):
    with pytest.raises(ValueError):
        call()
