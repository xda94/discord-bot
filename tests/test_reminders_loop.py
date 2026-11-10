import asyncio
import time
from types import SimpleNamespace

import db
from db import reminders
from features import reminders as feature_module
from features.reminders import RemindersFeature


def _set(rid, **values):
    with db._connect(commit=True) as c:
        c.execute('UPDATE reminders SET '+','.join(k+'=?' for k in values)+' WHERE id=?', (*values.values(), rid))


def _feature():
    feature = object.__new__(RemindersFeature)
    feature.client = SimpleNamespace(user=SimpleNamespace(id=99), get_channel=lambda _: None)
    feature._reconciled = {}
    feature._next_cleanup = 0.0
    return feature


def test_list_uncertain_returns_only_uncertain_failed_rows(tmp_db):
    pending = reminders.create(1, 10, time.time()+100, 'pending')
    late = reminders.create(1, 10, time.time()+200, 'late uncertain')
    early = reminders.create(1, 10, time.time()+300, 'early uncertain')
    forbidden = reminders.create(1, 10, time.time()+400, 'forbidden')
    other_state = reminders.create(1, 10, time.time()+500, 'pending with error')
    _set(late, state='failed', last_error='uncertain-delivery', remind_at=50)
    _set(early, state='failed', last_error='uncertain-delivery', remind_at=20)
    _set(forbidden, state='failed', last_error='Forbidden')
    _set(other_state, last_error='uncertain-delivery')
    rows = reminders.list_uncertain()
    assert [row['id'] for row in rows] == [early, late]
    assert rows[0] == reminders.get(early)
    assert pending not in [row['id'] for row in rows]


def test_consecutive_passes_clean_up_once_and_skip_full_scan(tmp_db, monkeypatch):
    calls = []
    real_cleanup = reminders.cleanup
    monkeypatch.setattr(feature_module.store, 'cleanup', lambda *a, **k: (calls.append(1), real_cleanup(*a, **k)))
    def no_full_scan(*_args, **_kwargs):
        raise AssertionError('list_for must not be used by the loop')
    monkeypatch.setattr(feature_module.store, 'list_for', no_full_scan)
    feature = _feature()
    asyncio.run(RemindersFeature._check.coro(feature))
    asyncio.run(RemindersFeature._check.coro(feature))
    assert calls == [1]
    feature._next_cleanup = time.monotonic()-1
    asyncio.run(RemindersFeature._check.coro(feature))
    assert calls == [1, 1]


def test_expired_reconciliation_entries_are_dropped(tmp_db):
    feature = _feature()
    feature._reconciled = {1: time.monotonic()-1, 2: time.monotonic()+3600}
    asyncio.run(RemindersFeature._check.coro(feature))
    assert feature._reconciled == {2: feature._reconciled[2]}


def test_init_sets_loop_state():
    tree = SimpleNamespace(command=lambda **_: (lambda f: f))
    feature = RemindersFeature(SimpleNamespace(), tree)
    assert feature._reconciled == {} and feature._next_cleanup == 0.0
    assert RemindersFeature._reconciled == {}
