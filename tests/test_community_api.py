import json
import time

import pytest

from db import community
from db.connection import _connect
from web.app import create_app

GUILD = 1234567890123456789
USER = 2234567890123456789
HEADERS = {'Authorization': 'Bearer test-token', 'X-Discord-ID-Format': 'string'}


@pytest.fixture
def client(tmp_db):
    return create_app({'TESTING': True, 'API_TOKEN': 'test-token'}).test_client()


def session():
    row = community.create_session(GUILD, USER, USER, 'sedinta', 'en', {'subject': 'A topic', 'options': ['One', 'Two']})
    community.bind_session_message(GUILD, row['id'], USER)
    return row


def quote(approved=False, index=0):
    row = community.create_quote_request(GUILD, USER, USER + index, USER, USER, 'private pending text', '<author>', 'ro')
    if approved:
        community.approve_quote(GUILD, row['id'], USER)
    return row


@pytest.mark.parametrize('route', ['sessions', 'bingo', 'quotes', 'titles'])
def test_authenticated_positive_guild(client, route):
    assert client.get(f'/community/{route}?guild_id={GUILD}').status_code == 401
    assert client.get(f'/community/{route}?guild_id={GUILD}', headers={'Authorization': 'Bearer bad'}).status_code == 401
    for guild in ['', '0', '-1', 'true', '9223372036854775808']:
        assert client.get(f'/community/{route}?guild_id={guild}', headers=HEADERS).status_code == 400
    assert client.get(f'/community/{route}?guild_id={GUILD}', headers=HEADERS).status_code == 200


def test_sessions_tallies_ids_cancel(client):
    row = session()
    community.cast_vote(GUILD, row['id'], USER + 1, '0')
    response = client.get(f'/community/sessions?guild_id={GUILD}', headers=HEADERS).get_json()
    item = response['sessions'][0]
    assert item['guild_id'] == str(GUILD)
    assert item['creator_id'] == str(USER)
    assert item['tally'] == {'0': 1, '1': 0}
    assert str(USER + 1) not in json.dumps(response)
    path = f"/community/sessions/{row['id']}/cancel"
    assert client.post(path, json={'guild_id': str(GUILD + 1)}, headers=HEADERS).status_code == 404
    assert client.post(path, json={'guild_id': str(GUILD)}, headers=HEADERS).status_code == 200
    assert community.cast_vote(GUILD, row['id'], USER + 2, '1')['status'] == 'closed'
    assert client.post(path, json={'guild_id': GUILD}, headers=HEADERS).status_code == 409


def test_quote_privacy_real_count_and_revoke(client):
    approved = quote(True)
    for index in range(1, 103):
        quote(index=index)
    quote(index=103)
    with _connect(commit=True) as cursor:
        cursor.execute('UPDATE museum_quotes SET consent_expires_at=? WHERE source_message_id=?', (time.time() - 1, USER + 103))
    response = client.get(f'/community/quotes?guild_id={GUILD}', headers=HEADERS).get_json()
    assert response['pending_count'] == 102
    assert len(response['quotes']) == 1
    assert response['quotes'][0]['author_id'] == str(USER)
    assert 'consent_message_id' not in response['quotes'][0]
    community.create_session(GUILD, USER, USER, 'quiz', 'en', {'quote_id': approved['id'], 'quote_text': approved['quote_text'], 'correct_author_id': USER, 'candidates': [{'user_id': USER, 'name': 'A'}, {'user_id': USER + 1, 'name': 'B'}]})
    path = f"/community/quotes/{approved['id']}"
    assert client.delete(path, json={'guild_id': GUILD + 1}, headers=HEADERS).status_code == 404
    assert client.delete(path, json={'guild_id': GUILD}, headers=HEADERS).status_code == 200
    assert community.list_sessions(GUILD)[0]['phase'] == 'cancelled'
    response = client.get(f'/community/quotes?guild_id={GUILD}', headers=HEADERS).get_json()
    assert response['quotes'] == []
    assert 'private pending text' not in json.dumps(response)
    assert client.post('/community/quotes/1/approve', json={'guild_id': GUILD}, headers=HEADERS).status_code == 404


def test_habit_validation_mutations_minimum_and_snapshot(client):
    board = client.get(f'/community/bingo?guild_id={GUILD}', headers=HEADERS).get_json()['board']
    for payload in [{'guild_id': GUILD}, {'guild_id': GUILD, 'text_en': 'x' * 201, 'text_ro': 'ok'}, {'guild_id': GUILD, 'text_en': True, 'text_ro': 'ok'}, {'guild_id': GUILD, 'text_en': '', 'text_ro': 'ok'}]:
        assert client.post('/community/bingo/habits', json=payload, headers=HEADERS).status_code == 400
    response = client.post('/community/bingo/habits', json={'guild_id': str(GUILD), 'text_en': '<English>', 'text_ro': '<Română>'}, headers=HEADERS)
    assert response.status_code == 200
    habit = response.get_json()['habit']
    path = f"/community/bingo/habits/{habit['id']}"
    assert client.put(path, json={'guild_id': GUILD + 1, 'text_en': 'one', 'text_ro': 'doi'}, headers=HEADERS).status_code == 404
    assert client.delete(path, json={'guild_id': GUILD + 1}, headers=HEADERS).status_code == 404
    assert client.put(path, json={'guild_id': GUILD, 'text_en': 'one', 'text_ro': 'doi'}, headers=HEADERS).status_code == 200
    assert client.delete(path, json={'guild_id': GUILD}, headers=HEADERS).status_code == 200
    habits = community.list_habits(GUILD)
    for item in habits[:3]:
        assert client.delete(f"/community/bingo/habits/{item['id']}", json={'guild_id': GUILD}, headers=HEADERS).status_code == 200
    assert client.delete(f"/community/bingo/habits/{habits[3]['id']}", json={'guild_id': GUILD}, headers=HEADERS).status_code == 409
    assert client.get(f'/community/bingo?guild_id={GUILD}', headers=HEADERS).get_json()['board']['cells'] == board['cells']


def test_titles_validation_guild_scope(client):
    with _connect(commit=True) as cursor:
        cursor.execute('INSERT INTO community_titles(guild_id,user_id,title_key,awarded_at) VALUES (?,?,?,?)', (GUILD, USER, 'hero', time.time()))
    response = client.get(f'/community/titles?guild_id={GUILD}', headers=HEADERS).get_json()
    assert response['titles'][0]['user_id'] == str(USER)
    path = f'/community/titles/{USER}/hero'
    assert client.delete(path, json={'guild_id': GUILD + 1}, headers=HEADERS).status_code == 404
    assert client.delete(path, json={'guild_id': GUILD}, headers=HEADERS).status_code == 200
    for user, key in [('0', 'hero'), ('9223372036854775808', 'hero'), (str(USER), 'X' * 81), (str(USER), 'bad!')]:
        assert client.delete(f'/community/titles/{user}/{key}', json={'guild_id': GUILD}, headers=HEADERS).status_code == 400


def test_finite_json_bounds_and_safe_database_error(client, monkeypatch):
    assert client.post('/community/bingo/habits', data='{"guild_id":88,"text_en":NaN,"text_ro":"ok"}', content_type='application/json', headers=HEADERS).status_code == 400
    assert client.post('/community/bingo/habits', json=[], headers=HEADERS).status_code == 400
    assert client.post('/community/bingo/habits', headers=HEADERS).status_code == 400
    for limit in ['0', '101', 'bad']:
        assert client.get(f'/community/sessions?guild_id={GUILD}&limit={limit}', headers=HEADERS).status_code == 400
    def failed(*args, **kwargs):
        raise RuntimeError('secret quote, accusation, or database location')
    monkeypatch.setattr(community, 'list_sessions', failed)
    response = client.get(f'/community/sessions?guild_id={GUILD}', headers=HEADERS)
    assert response.status_code == 503
    assert response.get_json() == {'error': 'Community data is temporarily unavailable. Please try again.'}


@pytest.mark.parametrize('method,path', [
    ('post', '/community/sessions/1/cancel'),
    ('post', '/community/bingo/habits'),
    ('put', '/community/bingo/habits/1'),
    ('delete', '/community/bingo/habits/1'),
    ('delete', '/community/quotes/1'),
    ('delete', f'/community/titles/{USER}/hero'),
])
def test_mutation_authentication_and_guild_validation(client, method, path):
    mutate = getattr(client, method)
    payload = {'guild_id': GUILD, 'text_en': 'one', 'text_ro': 'doi'}
    assert mutate(path, json=payload).status_code == 401
    assert mutate(path, json=payload, headers={'Authorization': 'Bearer wrong'}).status_code == 401
    for guild in [None, 0, True, '9223372036854775808']:
        assert mutate(path, json={**payload, 'guild_id': guild}, headers=HEADERS).status_code == 400


def test_pending_and_revoked_quote_not_exposed_by_session_api(client, monkeypatch):
    approved = quote(True)
    row = community.create_session(GUILD, USER, USER, 'quiz', 'en', {'quote_id': approved['id'], 'quote_text': approved['quote_text'], 'correct_author_id': USER, 'candidates': [{'user_id': USER, 'name': 'A'}, {'user_id': USER + 1, 'name': 'B'}]})
    response = client.get(f'/community/sessions?guild_id={GUILD}', headers=HEADERS).get_json()['sessions'][0]
    assert response['payload']['correct_author_id'] == str(USER)
    assert response['payload']['candidates'][0]['user_id'] == str(USER)
    community.delete_quote(GUILD, approved['id'])
    monkeypatch.setattr(community, 'list_sessions', lambda *args, **kwargs: [row])
    response = client.get(f'/community/sessions?guild_id={GUILD}', headers=HEADERS).get_json()['sessions'][0]
    assert response['payload'] == {}
    assert response['result_text'] is None
    assert approved['quote_text'] not in json.dumps(response)


def test_default_response_ids_preserved_and_all_nested_ids_precise(client):
    board = community.get_or_create_board(GUILD)
    community.mark_bingo_cell(GUILD, board['id'], 0, USER)
    community.confirm_bingo_cell(GUILD, board['id'], 0, USER + 1)
    payload = client.get(f'/community/bingo?guild_id={GUILD}', headers=HEADERS).get_json()
    assert payload['board']['marks'][0]['marker_id'] == str(USER)
    assert payload['board']['marks'][0]['confirmer_id'] == str(USER + 1)
    assert client.get(f'/community/bingo?guild_id={GUILD}', headers={'Authorization': 'Bearer test-token'}).get_json()['guild_id'] == GUILD
