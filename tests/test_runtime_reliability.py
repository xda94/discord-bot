import ast
import asyncio
import logging
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import db
from db import notifications, reminders
from features.flights import FlightTrackerFeature, _select_trackers_for_pass
from flight_provider import FlightOffer
from notifications import NotificationsFeature


def test_recurring_recovery_clears_terminal_timestamp_and_survives_cleanup(tmp_db):
    rid=reminders.create(1,10,time.time()+60,'call',recurrence='daily')
    with db._connect(commit=True) as c:
        c.execute("UPDATE reminders SET remind_at=1,state='failed',finished_at=2,last_error='uncertain-delivery' WHERE id=?",(rid,))
    reminders.complete(reminders.get(rid),message_id=99,now=100)
    row=reminders.get(rid)
    assert row['state']=='pending' and row['finished_at'] is None
    reminders.cleanup(now=40*86400)
    assert reminders.get(rid)['state']=='pending'


def test_cleanup_retains_legacy_pending_reminder_with_stale_timestamp(tmp_db):
    rid=reminders.create(1,10,time.time()+60,'call',recurrence='weekly')
    with db._connect(commit=True) as c:
        c.execute('UPDATE reminders SET finished_at=1 WHERE id=?',(rid,))
    reminders.cleanup(now=40*86400)
    assert reminders.get(rid)


@pytest.mark.parametrize('budget',[None,500])
def test_flight_enqueue_failure_rolls_back_price_and_history_then_retries(tmp_db,monkeypatch,budget):
    start=(date.today()+timedelta(days=60)).isoformat()
    end=(date.today()+timedelta(days=70)).isoformat()
    rid=db.add_flight_tracker(1,'OTP','BKK',start,end,budget=budget)
    db.update_flight_tracker_result(rid,600,'EUR',start,end,checked_at=0)
    feature=object.__new__(FlightTrackerFeature)
    feature.client=SimpleNamespace(fetch_user=AsyncMock(side_effect=RuntimeError('DM unavailable')))
    offer=FlightOffer(450,'EUR',start,end,(),0)
    feature._search_and_persist=AsyncMock(return_value=offer)
    original=notifications.enqueue
    monkeypatch.setattr(notifications,'enqueue',lambda **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError('locked')))
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(feature._process_tracker(db.get_flight_tracker(rid)))
    row=db.get_flight_tracker(rid)
    assert row['last_price']==600 and row['last_checked_at']==0
    assert not row['budget_alerted']
    assert db.get_flight_price_history(rid)==[]
    assert notifications.summary()=={}
    monkeypatch.setattr(notifications,'enqueue',original)
    asyncio.run(feature._process_tracker(row))
    row=db.get_flight_tracker(rid)
    assert row['last_price']==450
    assert bool(row['budget_alerted'])==(budget is not None)
    assert len(db.get_flight_price_history(rid))==1
    assert notifications.summary()=={'pending':1}


def test_expired_and_retired_flight_trackers_do_not_occupy_user_quota(tmp_db):
    past=(date.today()-timedelta(days=1)).isoformat()
    start=(date.today()+timedelta(days=60)).isoformat()
    end=(date.today()+timedelta(days=70)).isoformat()
    db.add_flight_tracker(1,'OTP','BKK',past,end)
    db.add_flight_tracker(1,'OTP','LHR',start,end,trip_days=7)
    db.add_flight_tracker(1,'OTP','CDG','invalid',end)
    active=db.add_flight_tracker(1,'OTP','JFK',start,end)
    assert [row['id'] for row in _select_trackers_for_pass(db.get_all_flight_trackers())]==[active]


def test_notification_tick_recovers_after_transient_database_error(tmp_db,monkeypatch,caplog):
    original=notifications.claim
    monkeypatch.setattr(notifications,'claim',lambda **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError('locked')))
    feature=NotificationsFeature(SimpleNamespace(fetch_user=AsyncMock()))
    asyncio.run(feature._check.coro(feature))
    assert 'Notification delivery loop failed' in caplog.text
    monkeypatch.setattr(notifications,'claim',original)
    notifications.enqueue(1,'flight',1,'price','offer','en',0)
    user=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=1)))
    feature.client.fetch_user.return_value=user
    asyncio.run(feature._check.coro(feature))
    assert notifications.summary()=={'sent':1}


def test_ready_sync_and_feature_failures_do_not_block_other_startup():
    source=ast.parse((Path(__file__).resolve().parents[1]/'bot.py').read_text())
    function=next(node for node in source.body if isinstance(node,ast.AsyncFunctionDef) and node.name=='on_ready')
    function.decorator_list=[]
    module=ast.Module(body=[function],type_ignores=[])
    first=SimpleNamespace(start_tasks=AsyncMock(side_effect=RuntimeError('failed')))
    second=SimpleNamespace(start_tasks=AsyncMock())
    catalog=AsyncMock(side_effect=RuntimeError('failed'))
    namespace=dict(tree=SimpleNamespace(sync=AsyncMock(side_effect=RuntimeError('failed'))),
                   refresh_command_catalog=catalog,BACKGROUND_FEATURES=[first,second],
                   logger=logging.getLogger('test-ready'),client=SimpleNamespace(user=SimpleNamespace(id=1)))
    exec(compile(module,'bot.py','exec'),namespace)
    asyncio.run(namespace['on_ready']())
    catalog.assert_awaited_once()
    first.start_tasks.assert_awaited_once()
    second.start_tasks.assert_awaited_once()
