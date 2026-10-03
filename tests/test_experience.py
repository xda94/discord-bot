"""Behavioral coverage for bilingual actions and durable delivery."""
import asyncio
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from string import Formatter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import db
from db import reminders, notifications
from assistant_profiles import effective_profile, AssistantProfile
from command_time import duration, calendar_time, reminder_time, next_occurrence, number
from i18n import CATALOG, language_for
from natural_commands import parse_natural_command, NaturalCommandError
from features.natural_commands import NaturalCommandsFeature
from features.reminders import RemindersFeature, ReminderControls
from features.wishlist import WishlistFeature
from notifications import delivery_at, drain
from action_proposals import validate_proposal, authorize_proposal

CORPUS=json.loads((Path(__file__).parent.parent/'data/natural-command-eval.json').read_text())


@pytest.mark.parametrize('case',CORPUS,ids=lambda case:case['text'])
def test_reviewed_bilingual_corpus(case):
    result=parse_natural_command(case['text'])
    kind='conversation' if result is None else type(result).__name__
    assert kind==case['expected']


@pytest.mark.parametrize('text,seconds',[
    ('o oră',3600),('1 oră și 30 de minute',5400),('jumătate de oră',1800),
    ('un sfert de oră',900),('half an hour',1800),('2 weeks',1209600),('30m',1800),
    ('1.5h',5400),('0 minutes',None),('1 hour garbage',None),('2h30m',None),
])
def test_complete_duration(text,seconds):
    assert duration(text)==seconds


def test_original_reminder_text_and_url_are_preserved():
    assert parse_natural_command('adu-mi aminte peste o oră să Sun Acasă.').text=='Sun Acasă.'
    url='https://Example.com/Știre?ID=ABC'
    assert parse_natural_command('urmărește '+url+'.').url==url
    result=parse_natural_command('amintește-mi peste 1 oră și 30 de minute să sun acasă')
    assert result.seconds==5400 and result.text=='sun acasă'
    assert isinstance(parse_natural_command('amintește-mi peste 1 oră și 30 de minute'),NaturalCommandError)


@pytest.mark.parametrize('text,lang,expected',[
    ('1500,50','ro',1500.5),('1.500,50','ro',1500.5),('1,500.50','en',1500.5),
    ('1.500','ro',None),('1,500','en',None),('NaN','en',None),('inf','en',None),
])
def test_unambiguous_money(text,lang,expected):
    assert number(text,lang)==expected


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


def test_calendar_requires_exact_future_and_unique_time():
    now=stamp('2026-03-27T12:00:00+00:00')
    assert calendar_time('mâine la 9','Europe/Bucharest',now=now)==stamp('2026-03-28T09:00:00+02:00')
    assert calendar_time('azi la 9','Europe/Bucharest',now=now) is None
    assert calendar_time('vineri seara','Europe/Bucharest',now=now) is None
    assert calendar_time('2026-03-29 03:30','Europe/Bucharest',now=now) is None
    assert calendar_time('2026-10-25 03:30','Europe/Bucharest',now=now) is None


def test_recurring_wall_clock_and_weekday_restart_coalescing():
    previous=stamp('2026-03-27T09:00:00+02:00')
    now=stamp('2026-03-30T11:00:00+03:00')
    assert next_occurrence(previous,'weekdays','Europe/Bucharest',now)==stamp('2026-03-31T09:00:00+03:00')
    assert reminder_time('la 9','Europe/Bucharest',recurrence='weekdays',now=stamp('2026-03-28T12:00:00+02:00'))==stamp('2026-03-30T09:00:00+03:00')


def test_language_precedence_and_catalog_placeholders():
    assert language_for('arată-mi lista',{'language':'en'})=='en'
    assert language_for('answer in Romanian',{'language':'en'})=='ro'
    assert language_for('answer in English',{'language':'ro'})=='en'
    assert language_for('arată-mi lista')=='ro'
    assert language_for('ok')=='en'
    for en,ro in CATALOG.values():
        fields=lambda text:{name for _,name,_,_ in Formatter().parse(text) if name}
        assert fields(en)==fields(ro)


def test_additive_migration_and_explicit_timezone(tmp_db):
    # Simulate profile values left by the old application.
    with db._connect(commit=True) as c:
        c.execute("INSERT INTO assistant_profiles(user_id,timezone) VALUES(1,'Europe/Bucharest'),(2,'UTC')")
        c.execute('INSERT INTO reminders(user_id,channel_id,remind_at,message) VALUES(1,10,9999999999,"legacy")')
    db.init_db()
    db.init_db()
    assert db.get_assistant_profile(1)['timezone_configured']==1
    assert db.get_assistant_profile(2)['timezone_configured']==0
    assert reminders.list_for(1)[0]['creator_id']==1
    assert db.set_assistant_profile(2,timezone='UTC')['timezone_configured']==1
    assert db.set_assistant_profile(3,delivery_mode='daily') is None
    assert db.set_assistant_profile(3,timezone='UTC',delivery_mode='daily')
    assert db.set_assistant_profile(3,quiet_start='22:00') is None


def test_reminder_claim_retry_and_ownership(tmp_db):
    rid=reminders.create(1,10,time.time()+1,'call',creator_id=2)
    with db._connect(commit=True) as c:
        c.execute('UPDATE reminders SET remind_at=1 WHERE id=?',(rid,))
    row=reminders.claim_due(now=100)[0]
    assert reminders.claim_due(now=100)==[]
    reminders.fail(row,'HTTPException',now=100)
    assert reminders.get(rid)['next_attempt']==160
    assert reminders.claim_due(now=159)==[]
    row=reminders.claim_due(now=160)[0]
    reminders.fail(row,'HTTPException',now=160)
    assert reminders.get(rid)['next_attempt']==460
    row=reminders.claim_due(now=460)[0]
    reminders.fail(row,'HTTPException',now=460)
    assert reminders.get(rid)['next_attempt']==1360
    row=reminders.claim_due(now=1360)[0]
    reminders.fail(row,'HTTPException',now=1360)
    assert reminders.get(rid)['state']=='failed'
    assert reminders.retry(rid,999) is False
    assert reminders.retry(rid,2)
    assert reminders.cancel(rid,999) is False
    assert reminders.cancel(rid,1)


def test_uncertain_claim_not_resent_and_snooze_is_separate(tmp_db):
    rid=reminders.create(1,10,time.time()+1,'call',recurrence='daily')
    with db._connect(commit=True) as c:
        c.execute('UPDATE reminders SET remind_at=1 WHERE id=?',(rid,))
    reminders.claim_due(now=100)
    assert reminders.claim_due(now=401)==[]
    row=reminders.get(rid)
    assert row['state']=='failed' and row['last_error']=='uncertain-delivery'
    snoozed=reminders.snooze(rid,1)
    assert snoozed!=rid and reminders.get(snoozed)['recurrence'] is None
    assert reminders.get(rid)['recurrence']=='daily'


def message(text,event_id=100,user_id=1,channel_id=10,mentioned=True):
    return SimpleNamespace(id=event_id,content=f'<@99> {text}' if mentioned else text,clean_content=text,
        mentions=[SimpleNamespace(id=99,display_name='Bot')] if mentioned else [],role_mentions=[],channel_mentions=[],
        author=SimpleNamespace(id=user_id,send=AsyncMock()),channel=SimpleNamespace(id=channel_id),guild=None,
        reference=None,reply=AsyncMock(),add_reaction=AsyncMock())


def natural_feature():
    return NaturalCommandsFeature(None,bot_id=99,wishlist=object.__new__(WishlistFeature),flights=None,reminders=RemindersFeature)


def test_timezone_clarification_and_message_dedup(tmp_db):
    feature=natural_feature()
    first=message('amintește-mi mâine la 9 să sun acasă')
    assert asyncio.run(feature.handle_message(first))
    assert reminders.list_for(1)==[]
    first.add_reaction.assert_not_awaited()
    assert 'fus orar' in first.reply.await_args.args[0]
    other=message('Europe/Bucharest',101,user_id=2)
    assert asyncio.run(feature.handle_message(other)) is False
    missing=message('Europe/Bucharest',102,mentioned=False)
    assert asyncio.run(feature.handle_message(missing)) is False
    follow=message('Europe/Bucharest',103)
    assert asyncio.run(feature.handle_message(follow))
    assert len(reminders.list_for(1))==1
    assert asyncio.run(feature.handle_message(follow)) is False
    follow.add_reaction.assert_awaited_once_with('✅')
    duplicate=message('adu-mi aminte peste o oră să sun acasă',104)
    asyncio.run(feature.handle_message(duplicate))
    asyncio.run(feature.handle_message(duplicate))
    assert len(reminders.list_for(1))==2
    duplicate.add_reaction.assert_awaited_once_with('✅')


def test_acknowledgment_failure_does_not_repeat_action(tmp_db):
    feature=natural_feature()
    msg=message('remind me in one hour to stretch')
    msg.add_reaction.side_effect=RuntimeError('no reaction permission')
    asyncio.run(feature.handle_message(msg))
    asyncio.run(feature.handle_message(msg))
    assert len(reminders.list_for(1))==1


def test_owner_scoped_product_resolution_and_price_target(tmp_db):
    db.add_scraped_item(1,'https://example.com/headphones','Căști',100,True,'RON')
    db.add_scraped_item(2,'https://example.com/private','Private item',100,True,'RON')
    feature=natural_feature()
    msg=message('anunță-mă când căști scad sub 50 lei')
    asyncio.run(feature.handle_message(msg))
    with db._connect() as c:
        assert c.execute('SELECT target_price FROM scraped_items WHERE user_id=1').fetchone()[0]==50
        assert c.execute('SELECT target_price FROM scraped_items WHERE user_id=2').fetchone()[0] is None
    unauthorized=message('nu mai urmări #2',101)
    asyncio.run(feature.handle_message(unauthorized))
    unauthorized.add_reaction.assert_not_awaited()
    assert len(db.get_user_scraped_items(2))==1


@pytest.mark.parametrize('proposal',[
    {'intent':'execute_python','slots':{}},
    {'intent':'target','slots':{'reference':'x','price':True,'currency':'RON'}},
    {'intent':'graph','slots':{'days':True}},
    {'intent':'delete_item','slots':{'reference':'x','user_id':2}},
    {'intent':'restock','slots':{'reference':'x','enabled':'yes'}},
    {'intent':'reminder_cancel','slots':{'reminder_id':True}},
])
def test_invalid_proposals(proposal):
    with pytest.raises(ValueError):
        validate_proposal(proposal)


@pytest.mark.parametrize('text,proposal',[
    ('salut',{'intent':'track','slots':{'reference':'https://invented.example/'}}),
    ('nu urmări https://example.com/item',{'intent':'track','slots':{'reference':'https://example.com/item'}}),
    ('"track https://example.com/item"',{'intent':'track','slots':{'reference':'https://example.com/item'}}),
    ('remind me tomorrow to call',{'intent':'reminder','slots':{'when':'tomorrow at 9','text':'call'}}),
    ('anunță-mă când căști scad sub 500 lei',{'intent':'target','slots':{'reference':'căști','price':600,'currency':'RON'}}),
])
def test_invented_or_unauthorized_operands_make_no_writes(tmp_db,text,proposal):
    feature=natural_feature()
    msg=message(text)
    asyncio.run(feature.execute_proposal(msg,proposal))
    assert reminders.list_for()==[]
    msg.add_reaction.assert_not_awaited()
    with db._connect() as c:
        assert c.execute('SELECT COUNT(*) FROM action_executions').fetchone()[0]==0


def test_button_checks_ownership(tmp_db):
    rid=reminders.create(1,10,time.time()+100,'call')
    async def run():
        view=ReminderControls(rid)
        interaction=SimpleNamespace(id=1,user=SimpleNamespace(id=2),response=SimpleNamespace(send_message=AsyncMock(),defer=AsyncMock()))
        await view.children[1].callback(interaction)
        assert reminders.get(rid)['state']=='pending'
        assert 'another user' in interaction.response.send_message.await_args.args[0]
    asyncio.run(run())


def test_quiet_hours_digest_and_coalescing(tmp_db):
    now=stamp('2026-10-03T23:00:00+03:00')
    profile=AssistantProfile(timezone='Europe/Bucharest',timezone_configured=True,quiet_start='22:00',quiet_end='08:00')
    assert delivery_at(profile,now)==stamp('2026-10-04T08:00:00+03:00')
    digest=AssistantProfile(timezone='Europe/Bucharest',timezone_configured=True,delivery_mode='daily',digest_time='09:00')
    assert delivery_at(digest,now)==stamp('2026-10-04T09:00:00+03:00')
    first=notifications.enqueue(1,'wishlist',1,'price','old','en',10,observed_at=1)
    assert notifications.enqueue(1,'wishlist',1,'price','new','en',10,observed_at=2)==first
    notifications.enqueue(1,'wishlist',1,'target','target reached','en',10,observed_at=3)
    rows=notifications.claim(now=10)
    assert len(rows)==2 and rows[0]['body']=='new'
    assert notifications.claim(now=10)==[]
    notifications.finish(rows,'Forbidden',permanent=True,now=10)
    assert notifications.summary()=={'failed':2}
    assert notifications.retry(first,999) is False
    assert notifications.retry(first,1)


def test_failed_dm_survives_restart_and_retry(tmp_db):
    notifications.enqueue(1,'flight',1,'price','offer','en',0)
    user=SimpleNamespace(send=AsyncMock(side_effect=RuntimeError('transport failed')))
    client=SimpleNamespace(fetch_user=AsyncMock(return_value=user))
    asyncio.run(drain(client))
    assert notifications.summary()=={'pending':1}
    with db._connect(commit=True) as c:
        c.execute('UPDATE notification_outbox SET due_at=0')
    user.send.side_effect=None
    asyncio.run(drain(client))
    assert notifications.summary()=={'sent':1}
    assert 'Observed at' in user.send.await_args.args[0]


def test_reminder_text_spacing_and_language_override():
    from natural_commands import ShowWishlistAction
    result = parse_natural_command('te rog adu-mi   aminte peste o oră să Sun  Acasă.\nIa cheile!')
    assert result.text == 'Sun  Acasă.\nIa cheile!'
    assert isinstance(parse_natural_command('show my wishlist in Romanian'), ShowWishlistAction)
    assert parse_natural_command('remind me in one hour to write in English').text == 'write in English'
    assert parse_natural_command('what happens then?') is None
    assert isinstance(parse_natural_command('remind me in one hour to call and show my wishlist'), NaturalCommandError)


def test_clarification_rejects_unrelated_text_and_accepts_explicit_field(tmp_db):
    feature = natural_feature()
    first = message('remind me in one hour')
    asyncio.run(feature.handle_message(first))
    assert (1, 10) in feature.pending
    assert asyncio.run(feature.handle_message(message('how are you?', 101))) is False
    assert reminders.list_for(1) == []
    assert asyncio.run(feature.handle_message(message('text: Call  home.', 102)))
    assert reminders.list_for(1)[0]['message'] == 'Call  home.'


def test_clarification_cancel_channel_isolation_and_expiry(tmp_db):
    feature = natural_feature()
    asyncio.run(feature.handle_message(message('remind me in one hour')))
    assert asyncio.run(feature.handle_message(message('text: wrong channel', 101, channel_id=11))) is False
    asyncio.run(feature.handle_message(message('cancel', 102)))
    assert feature.pending == {} and reminders.list_for() == []
    asyncio.run(feature.handle_message(message('remind me in one hour', 103)))
    waiting = feature.pending[(1, 10)]
    feature.pending[(1, 10)] = (0, *waiting[1:])
    asyncio.run(feature._expire.coro(feature))
    assert feature.pending == {}
    with db._connect() as c:
        outcomes = [r[0] for r in c.execute("SELECT activity FROM analytics_daily WHERE activity LIKE '%/timeout' OR activity LIKE '%/abandoned'")]
    assert len(outcomes) == 2


@pytest.mark.parametrize('proposal,text', [
    ({'intent':'flights','slots':{}}, 'salut'),
    ({'intent':'wishlist','slots':{}}, 'show my flights'),
    ({'intent':'reminder','slots':{'when':'tomorrow at 9','text':'call','recurrence':'weekly'}}, 'remind me tomorrow at 9 to call'),
    ({'intent':'target','slots':{'reference':'headphones','price':500,'currency':'EUR'}}, 'notify me when headphones drop below 500 lei'),
    ({'intent':'graph','slots':{'reference':'#1','days':90}}, 'graph #1 for 30 days'),
    ({'intent':'restock','slots':{'reference':'#1','enabled':True}}, 'disable restock only for #1'),
])
def test_proposals_cannot_invent_semantics(proposal, text):
    validate_proposal(proposal)
    with pytest.raises(ValueError):
        authorize_proposal(proposal, text)


@pytest.mark.parametrize('bad', [{'intent': {}, 'slots':{}}, {'intent':'wishlist','slots':[]}, [], None])
def test_malformed_envelopes_do_not_escape_validation(bad):
    with pytest.raises(ValueError):
        validate_proposal(bad)


def test_model_action_envelope_and_conversation_cannot_claim_success():
    from llm.responses import _parse_mention_result
    action = {'intent':'reminder','slots':{'when':'one hour','text':'call'}}
    result = _parse_mention_result(json.dumps({'text':'','reaction':None,'action':action}), 'remind me in one hour to call', allow_action=True)
    assert result.action == action
    with pytest.raises(ValueError):
        _parse_mention_result(json.dumps({'text':'Saved.','reaction':None,'action':action}), 'remind me in one hour to call', allow_action=True)
    for text in ('I have saved your reminder.', 'Am salvat reminderul.'):
        with pytest.raises(ValueError):
            _parse_mention_result(json.dumps({'text':text,'reaction':None}), 'save a reminder')


def test_outbox_preserves_due_time_and_partial_progress(tmp_db):
    nid = notifications.enqueue(1,'flight',1,'price','x' * 2500,'en',0)
    notifications.enqueue(1,'flight',1,'price','x' * 2500,'en',9999999999)
    with db._connect() as c:
        assert c.execute('SELECT due_at FROM notification_outbox WHERE id=?',(nid,)).fetchone()[0] == 0
    user = SimpleNamespace(send=AsyncMock(side_effect=[SimpleNamespace(id=1), RuntimeError('temporary')]))
    client = SimpleNamespace(fetch_user=AsyncMock(return_value=user))
    asyncio.run(drain(client))
    with db._connect(commit=True) as c:
        assert c.execute('SELECT sent_parts,state FROM notification_outbox').fetchone() == (1,'pending')
        c.execute('UPDATE notification_outbox SET due_at=0')
    user.send.reset_mock()
    user.send.side_effect = None
    asyncio.run(drain(client))
    assert user.send.await_count == 1 and len(user.send.await_args.args[0]) < 1900
    assert notifications.summary() == {'sent':1}


def test_new_profile_schedule_reschedules_pending_alerts(tmp_db):
    notifications.enqueue(1,'flight',1,'price','offer','en',0)
    saved = db.set_assistant_profile(1,timezone='Europe/Bucharest',delivery_mode='daily',digest_time='09:00')
    assert saved
    with db._connect() as c:
        assert c.execute('SELECT due_at FROM notification_outbox').fetchone()[0] > time.time()
    assert db.set_assistant_profile(1,delivery_mode='immediate')
    with db._connect() as c:
        assert c.execute('SELECT due_at FROM notification_outbox').fetchone()[0] <= time.time()


def test_localization_preserves_titles_and_uses_current_saved_language(tmp_db):
    from wishlist.currency import CurrencyConverter
    db.add_scraped_item(1,'https://example.com/Price:', 'Price: In stock',100,True,'RON')
    db.set_assistant_profile(1,language='ro')
    feature = object.__new__(WishlistFeature)
    feature.converter = CurrencyConverter()
    output = feature.format_items_for_user(1)
    assert 'Price: In stock' in output[0] and 'Produsele urmărite' in output[0]
    assert 'Your tracked items' in feature.format_items_for_user(1,language='en')[0]
    notifications.enqueue(1,'wishlist',1,'target','Price: In stock\nTarget reached. Now 50 RON','en',0,protected=('Price: In stock',))
    user = SimpleNamespace(send=AsyncMock())
    asyncio.run(drain(SimpleNamespace(fetch_user=AsyncMock(return_value=user))))
    assert 'Price: In stock' in user.send.await_args.args[0]
    assert 'țintă' in user.send.await_args.args[0]


def test_flight_budget_crossing_and_failed_dm_keep_durable_alert(tmp_db):
    from features.flights import FlightTrackerFeature
    rid = db.add_flight_tracker(1,'OTP','BKK','2027-01-01','2027-01-10',0,1,'EUR',budget=500)
    feature = object.__new__(FlightTrackerFeature)
    feature.client = SimpleNamespace(fetch_user=AsyncMock(side_effect=RuntimeError('DM unavailable')))
    offer = SimpleNamespace(total_price=450,currency='EUR',departure_date='2027-01-01',return_date='2027-01-10',airlines=(),stops=0)
    feature._search_and_persist = AsyncMock(return_value=offer)
    asyncio.run(feature._process_tracker(db.get_flight_tracker(rid,1)))
    assert notifications.summary() == {'pending':1}
    assert db.get_flight_tracker(rid,1)['budget_alerted']
    asyncio.run(feature._process_tracker(db.get_flight_tracker(rid,1)))
    assert feature.client.fetch_user.await_count == 1
    offer.total_price = 550
    asyncio.run(feature._process_tracker(db.get_flight_tracker(rid,1)))
    assert not db.get_flight_tracker(rid,1)['budget_alerted']
    offer.total_price = 480
    asyncio.run(feature._process_tracker(db.get_flight_tracker(rid,1)))
    assert db.get_flight_tracker(rid,1)['budget_alerted']


def test_auth_and_lifecycle_api_contract(tmp_db):
    from web.app import create_app
    client = create_app({'TESTING':True,'API_TOKEN':'test-token'}).test_client()
    headers = {'Authorization':'Bearer test-token', 'X-Discord-ID-Format':'string'}
    assert client.get('/notifications').status_code == 401
    for payload in ({'timezone':42}, {'currency':{}}, {'quiet_start':'25:00'}):
        assert client.patch('/assistant-profiles/1',json=payload,headers=headers).status_code == 400
    response = client.post('/reminders/add',headers=headers,json={'user_id':'1','creator_id':'2','channel_id':'10','remind_at':time.time()+100,'message':'call'})
    assert response.status_code == 200
    rid = response.json['id']
    rows = client.get('/reminders/all',headers=headers).json
    assert rows[0]['creator_id'] == '2' and rows[0]['state'] == 'pending'
    assert client.patch(f'/reminders/{rid}',headers=headers,json={'message':'updated'}).status_code == 200
    assert client.patch(f'/reminders/{rid}',headers=headers,json={'timezone':'Invalid/Zone'}).status_code == 400
    assert client.post('/reminders/add',headers=headers,json=3).status_code == 400
    assert client.get('/analytics/natural?period=invalid',headers=headers).status_code == 400
    assert client.delete(f'/reminders/delete/{rid}',headers=headers).status_code == 200


def test_migration_against_actual_pre_change_schema(tmp_path,monkeypatch):
    from db import connection, experience_schema
    path = tmp_path/'legacy.db'
    monkeypatch.setattr(connection,'DB_FILE',str(path))
    with monkeypatch.context() as previous:
        previous.setattr(experience_schema,'migrate',lambda c:None)
        db.init_db()
    with db._connect(commit=True) as c:
        assert 'state' not in {r[1] for r in c.execute('PRAGMA table_info(reminders)')}
        c.execute('INSERT INTO reminders(user_id,channel_id,remind_at,message) VALUES(1,10,9999999999,"legacy")')
        c.execute("INSERT INTO assistant_profiles(user_id,timezone) VALUES(1,'Europe/Bucharest'),(2,'UTC')")
    db.init_db()
    db.init_db()
    assert reminders.list_for(1)[0]['state'] == 'pending'
    assert reminders.list_for(1)[0]['recurrence'] is None
    assert db.get_assistant_profile(1)['timezone_configured']
    assert not db.get_assistant_profile(2)['timezone_configured']


def test_airport_aliases_and_city_ambiguity():
    from airports import resolve_airport, search_airports
    assert any(r['iata_code']=='OTP' for r in search_airports('București'))
    with pytest.raises(ValueError):
        resolve_airport('București')
    assert resolve_airport('otp') == 'OTP'


@pytest.mark.parametrize('text', ['adu-mi aminte într-o oră să sun', 'adu mi aminte peste o oră să sun'])
def test_idiomatic_romanian_duration_variants(text):
    parsed = parse_natural_command(text)
    assert parsed.seconds == 3600 and parsed.text == 'sun'


def test_bare_wishlist_and_natural_reminder_text_edit(tmp_db):
    from natural_commands import ShowWishlistAction, NaturalClarification
    assert isinstance(parse_natural_command('lista de dorințe în lei'), ShowWishlistAction)
    assert isinstance(parse_natural_command('adu-mi aminte să sun acasă'), NaturalClarification)
    rid = reminders.create(1,10,time.time()+3600,'old')
    feature = natural_feature()
    asyncio.run(feature.handle_message(message(f'modifică textul reminderului #{rid} cu Sun  Acasă.')))
    assert reminders.get(rid)['message'] == 'Sun  Acasă.'


def test_local_calendar_api_uses_explicit_zone_and_rejects_gap(tmp_db):
    from web.app import create_app
    from zoneinfo import ZoneInfo
    client = create_app({'TESTING':True,'API_TOKEN':'t'}).test_client()
    headers = {'Authorization':'Bearer t'}
    payload = {'user_id':1,'channel_id':10,'message':'call','remind_at':2_000_000_000,'local_time':'2027-01-01T09:00'}
    assert client.post('/reminders/add',headers=headers,json=payload).status_code == 400
    payload['timezone']='Europe/Bucharest'
    response = client.post('/reminders/add',headers=headers,json=payload)
    assert response.status_code == 200
    row = reminders.get(response.json['id'])
    assert datetime.fromtimestamp(row['remind_at'],ZoneInfo(row['timezone'])).hour == 9
    assert client.patch(f"/reminders/{row['id']}",headers=headers,json={'local_time':'2027-03-28T03:30'}).status_code == 400


def test_model_actions_are_opt_in_and_single_generation(monkeypatch):
    from llm import responses
    monkeypatch.delenv('NATURAL_LLM_ENABLED',raising=False)
    assert not responses.natural_llm_enabled()
    monkeypatch.setenv('NATURAL_LLM_ENABLED','1')
    calls = []
    def query(**kwargs):
        calls.append(kwargs)
        return json.dumps({'text':'','reaction':None,'action':{'intent':'wishlist','slots':{}}})
    monkeypatch.setattr(responses.client,'query_llm',query)
    result = responses.generate_mention_result('Tester','show my wishlist',context_messages=[],model='discord-bot')
    assert result.action['intent'] == 'wishlist' and len(calls) == 1
    assert 'action' in calls[0]['response_schema']['required']
    with pytest.raises(ValueError):
        responses._parse_mention_result('{"text":"Hello","reaction":null}','hi',allow_action=True)


def test_dashboard_startup_elements_exist(tmp_db):
    import re
    from web.app import create_app
    client = create_app({'TESTING':True,'API_TOKEN':'t'}).test_client()
    page = client.get('/').data.decode()
    script = client.get('/static/dashboard.js').data.decode()
    initialization = script[script.index('  async function init()'):]
    ids = re.findall(r'\$\("#([\w-]+)"\)\.addEventListener',initialization)
    for element_id in ids:
        assert f'id="{element_id}"' in page
    assert 'id="reminder-edit-form"' in page and 'id="dashboard-language"' in page


@pytest.mark.parametrize('value,expected', [
    ('mâine la ora 9', '2026-03-28T09:00:00+02:00'),
    ('tomorrow at 9 pm', '2026-03-28T21:00:00+02:00'),
    ('tomorrow at noon', '2026-03-28T12:00:00+02:00'),
    ('mâine la miezul nopții', '2026-03-28T00:00:00+02:00'),
])
def test_calendar_clock_phrases(value, expected):
    assert calendar_time(value, 'Europe/Bucharest', now=stamp('2026-03-27T12:00:00+00:00')) == stamp(expected)


def test_ambiguous_target_price_followup_preserves_language_and_rejects_unrelated(tmp_db):
    url = 'https://example.com/headphones'
    item_id = db.add_scraped_item(1,url,'căști',2000,True,'RON')
    feature = natural_feature()
    first = message('anunță-mă când căști scad sub 1.500 lei')
    asyncio.run(feature.handle_message(first))
    assert (1,10) in feature.pending
    unrelated = message('1500 what is the weather',101)
    assert not asyncio.run(feature.handle_message(unrelated))
    assert (1,10) in feature.pending
    follow = message('1500 lei',102)
    asyncio.run(feature.handle_message(follow))
    assert db.get_scraped_item(1,url)[9] == 1500
    follow.add_reaction.assert_awaited_once_with('✅')


def test_timezone_followup_retains_creation_language(tmp_db):
    feature = natural_feature()
    asyncio.run(feature.handle_message(message('amintește-mi mâine la 9 să sun')))
    asyncio.run(feature.handle_message(message('Europe/Bucharest',101)))
    assert reminders.list_for(1)[0]['language'] == 'ro'


def test_concurrent_workers_claim_each_due_reminder_once(tmp_db):
    from concurrent.futures import ThreadPoolExecutor
    ids = [reminders.create(1,10,time.time()+100,f'item {i}') for i in range(8)]
    with db._connect(commit=True) as c:
        c.execute('UPDATE reminders SET remind_at=1')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: reminders.claim_due(now=100), range(4)))
    claimed = [row['id'] for batch in results for row in batch]
    assert sorted(claimed) == ids


def test_reminders_bypass_tracking_delivery_preferences(tmp_db):
    db.set_assistant_profile(1,timezone='Europe/Bucharest',delivery_mode='daily',digest_time='09:00',quiet_start='22:00',quiet_end='08:00')
    rid = reminders.create(1,10,time.time()+100,'sun acasă',language='ro')
    with db._connect(commit=True) as c:
        c.execute('UPDATE reminders SET remind_at=1 WHERE id=?',(rid,))
    channel = SimpleNamespace(id=10,guild=None,send=AsyncMock(return_value=SimpleNamespace(id=123)))
    feature = object.__new__(RemindersFeature)
    feature.client = SimpleNamespace(get_channel=lambda _: channel)
    asyncio.run(RemindersFeature._check.coro(feature))
    assert reminders.get(rid)['state'] == 'delivered'
    assert 'îți amintesc' in channel.send.await_args.args[0]
    assert notifications.summary() == {}


def test_new_feature_commands_register_with_runtime(tmp_db):
    import discord
    from discord import app_commands
    from features.flights import FlightTrackerFeature
    from features.assistant_profiles import AssistantProfilesFeature
    from features.onboarding import OnboardingFeature
    from features.help_feature import HelpFeature
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    for feature in (RemindersFeature, WishlistFeature, FlightTrackerFeature, AssistantProfilesFeature, OnboardingFeature, HelpFeature):
        feature(client, tree)
    names = {command.name for command in tree.get_commands()}
    assert {'start','help','remind','reminder-list','reminder-edit','reminder-cancel','reminder-snooze','flight-tracker-budget','wishlist-show'} <= names


def test_slash_redelivery_is_rejected_before_callback(tmp_db):
    import discord
    from analytics import AnalyticsCommandTree
    event = SimpleNamespace(id=123,type=discord.InteractionType.application_command,data={'name':'remind'},guild_id=None)
    assert asyncio.run(AnalyticsCommandTree.interaction_check(None,event))
    assert not asyncio.run(AnalyticsCommandTree.interaction_check(None,event))


def test_romanian_graph_preserves_product_content_and_localizes_chart(tmp_db,monkeypatch):
    from features import wishlist_graphs as graphs
    from wishlist import charts
    url = 'https://example.com/a'
    item_id = db.add_scraped_item(1,url,'Price: My English Title',100,True,'RON')
    db.add_price_history(item_id,100)
    db.add_price_history(item_id,90)
    specs = []
    monkeypatch.setattr(charts,'_png',lambda spec: specs.append(spec) or b'png')
    converter = SimpleNamespace(to_currency=lambda price,*_:price)
    text,file = graphs.build_graph(1,url,'RON',30,False,converter,lambda cur,_:cur,language='ro')
    file.close()
    assert '30' in text and 'zile' in text
    assert specs[0]['title']['text'] == 'Price: My English Title'
    assert 'Acum' in specs[0]['title']['subtitle'][1]
    assert specs[0]['layer'][0]['encoding']['y']['axis']['title'] == 'Preț (RON)'
    async def controls():
        view = graphs.WishlistGraphView(1,lambda *_:None,language='ro')
        assert view.children[0].label == '7 zile'
        assert view.children[3].label == 'Alt interval'
    asyncio.run(controls())


def test_auto_language_keeps_each_queued_alert_creation_language(tmp_db):
    notifications.enqueue(1,'flight',1,'price','Price dropped from 100 to 90 EUR','ro',0)
    notifications.enqueue(1,'flight',2,'price','Price dropped from 200 to 180 EUR','en',0)
    user = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=123)))
    asyncio.run(drain(SimpleNamespace(fetch_user=AsyncMock(return_value=user))))
    texts = [call.args[0] for call in user.send.await_args_list]
    assert 'Prețul a scăzut' in texts[0]
    assert 'Price dropped' in texts[1]


@pytest.mark.parametrize('proposal,text', [
    ({'intent':'track','slots':{'reference':'https://example.com/a'}},'could you track https://example.com/a and show my wishlist'),
    ({'intent':'track','slots':{'reference':'https://example.com/a'}},'could you track https://example.com/a but do not actually save it'),
    ({'intent':'reminder','slots':{'when':'one hour','text':'call'}},'could you remind me in one hour and thirty minutes to call'),
])
def test_model_cannot_execute_a_partial_or_denied_request(proposal,text):
    validate_proposal(proposal)
    with pytest.raises(ValueError):
        authorize_proposal(proposal,text)


def test_romanian_refresh_preserves_product_title(tmp_db):
    url = 'https://example.com/a'
    db.add_scraped_item(1,url,'Price: In stock',100,True,'RON')
    feature = natural_feature()
    feature.wishlist.refresh_items_for_user = AsyncMock(return_value=['Refreshed the requested item.\nRefreshed: **Price: In stock**\nPrice: 90 RON | In stock'])
    request = message('actualizează #1')
    asyncio.run(feature.handle_message(request))
    output = request.author.send.await_args.args[0]
    assert '**Price: In stock**' in output and 'Preț: 90 RON' in output
    assert 'Am actualizat produsul cerut.' in output


def test_calendar_edit_button_requires_explicit_timezone(tmp_db):
    rid = reminders.create(1,10,time.time()+100,'call')
    interaction = SimpleNamespace(id=200,user=SimpleNamespace(id=1),response=SimpleNamespace(send_message=AsyncMock(),send_modal=AsyncMock(),defer=AsyncMock()))
    async def run():
        view = ReminderControls(rid,'ro')
        await view.children[0].callback(interaction)
        interaction.response.send_modal.assert_not_awaited()
        assert 'fusul orar' in interaction.response.send_message.await_args.args[0]
        db.set_assistant_profile(1,timezone='UTC')
        await view.children[0].callback(interaction)
        interaction.response.send_modal.assert_awaited_once()
    asyncio.run(run())


def test_long_failed_reminder_list_stays_within_discord_limit(tmp_db):
    rid = reminders.create(1,10,time.time()+100,'x'*1900,language='ro')
    with db._connect(commit=True) as c:
        c.execute("UPDATE reminders SET state='failed',last_error='Forbidden' WHERE id=?",(rid,))
    async def run():
        chunks = RemindersFeature.format_for_user(1,language='ro')
        assert len(chunks) >= 2 and all(len(text) <= 1900 for text,_ in chunks)
        assert 'permisiuni' in chunks[-1][0]
        assert chunks[-1][1] is not None and chunks[0][1] is None
    asyncio.run(run())


@pytest.mark.parametrize('answer', ['Your reminder has been saved.', 'Reminderul a fost programat.', 'Done, your alert is saved.', 'Gata, am adăugat produsul.'])
def test_conversational_completion_claims_cannot_acknowledge_actions(answer):
    from llm.responses import _parse_mention_result
    with pytest.raises(ValueError):
        _parse_mention_result(json.dumps({'text':answer,'reaction':None}),'remind me later to call')


def test_romanian_auto_language_without_diacritics():
    assert language_for('salut ce mai faci') == 'ro'
    assert language_for('buna, multumesc') == 'ro'
    assert language_for('hello there') == 'en'


def test_uncertain_reminder_reconciles_an_uncached_channel(tmp_db):
    import discord
    rid = reminders.create(1,10,time.time()+100,'call')
    with db._connect(commit=True) as c:
        c.execute("UPDATE reminders SET state='failed',remind_at=1,last_error='uncertain-delivery' WHERE id=?",(rid,))
    embed = discord.Embed()
    embed.set_footer(text=f'reminder:{rid}:1')
    receipt = SimpleNamespace(id=321,author=SimpleNamespace(id=99),embeds=[embed])
    async def history(**kwargs):
        yield receipt
    channel = SimpleNamespace(id=10,history=history,send=AsyncMock())
    feature = object.__new__(RemindersFeature)
    feature.client = SimpleNamespace(user=SimpleNamespace(id=99),get_channel=lambda _:None,fetch_channel=AsyncMock(return_value=channel))
    asyncio.run(RemindersFeature._check.coro(feature))
    row = reminders.get(rid)
    assert row['state'] == 'delivered' and row['discord_message_id'] == 321
    channel.send.assert_not_awaited()
