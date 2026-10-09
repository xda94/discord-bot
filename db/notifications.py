"""Durable, coalesced tracking notifications."""
from __future__ import annotations
import time
import json
from db.connection import _connect


def enqueue(user_id,source,item_key,event_kind,body,language,due_at,*,observed_at=None,protected=(),cursor=None):
    observed=time.time() if observed_at is None else observed_at
    if cursor is not None:
        return _enqueue(cursor,user_id,source,item_key,event_kind,body,language,due_at,observed,protected)
    with _connect(commit=True) as c:
        c.execute('BEGIN IMMEDIATE')
        return _enqueue(c,user_id,source,item_key,event_kind,body,language,due_at,observed,protected)


def _enqueue(c,user_id,source,item_key,event_kind,body,language,due_at,observed,protected):
    existing=c.execute("SELECT id FROM notification_outbox WHERE user_id=? AND source=? AND item_key=? AND event_kind=? AND state='pending' AND sent_parts=0",(user_id,source,str(item_key),event_kind)).fetchone()
    if existing:
        c.execute('UPDATE notification_outbox SET delivery_body=NULL,body=?,language=?,observed_at=?,due_at=MIN(due_at,?),protected_json=? WHERE id=?',(body,language,observed,due_at,json.dumps(list(protected)),existing[0]))
        return existing[0]
    c.execute('INSERT INTO notification_outbox(user_id,source,item_key,event_kind,body,language,observed_at,due_at,protected_json) VALUES(?,?,?,?,?,?,?,?,?)',(user_id,source,str(item_key),event_kind,body,language,observed,due_at,json.dumps(list(protected))))
    return c.lastrowid


def defer(rows,due_at):
    with _connect(commit=True) as c:
        c.executemany("UPDATE notification_outbox SET state='pending',due_at=? WHERE id=? AND state='sending'",[(due_at,row['id']) for row in rows])


def claim(now=None,user_id=None):
    now=time.time() if now is None else now
    with _connect(commit=True) as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute("UPDATE notification_outbox SET state='failed',last_error='uncertain-delivery',finished_at=? WHERE state='sending' AND claimed_at<?",(now,now-300))
        query="SELECT id,user_id,body,language,observed_at,attempts,source,sent_parts,protected_json,delivery_body FROM notification_outbox WHERE state='pending' AND due_at<=?"
        args=[now]
        if user_id is not None:
            query+=' AND user_id=?'
            args.append(user_id)
        rows=[dict(zip(('id','user_id','body','language','observed_at','attempts','source','sent_parts','protected_json','delivery_body'),r)) for r in c.execute(query+' ORDER BY due_at LIMIT 100',args).fetchall()]
        for r in rows:
            c.execute("UPDATE notification_outbox SET state='sending',claimed_at=? WHERE id=?",(now,r['id']))
        return rows


def finish(rows,error=None,permanent=False,now=None):
    now=time.time() if now is None else now
    with _connect(commit=True) as c:
        for row in rows:
            attempts=row['attempts']+1
            terminal=permanent or attempts>3
            state='sent' if error is None else 'failed' if terminal else 'pending'
            c.execute('UPDATE notification_outbox SET state=?,attempts=?,last_error=?,due_at=?,finished_at=? WHERE id=?',(state,attempts,error,now+(60,300,900)[min(attempts-1,2)],now if state!='pending' else None,row['id']))
        c.execute('DELETE FROM notification_outbox WHERE finished_at<?',(now-30*86400,))


def summary(user_id=None):
    with _connect() as c:
        where=' WHERE user_id=?' if user_id is not None else ''
        return dict(c.execute('SELECT state,COUNT(*) FROM notification_outbox'+where+' GROUP BY state', [user_id] if user_id is not None else []).fetchall())


def retry(notification_id,user_id=None):
    with _connect(commit=True) as c:
        q="UPDATE notification_outbox SET state='pending',attempts=0,last_error=NULL,due_at=?,finished_at=NULL WHERE id=? AND state='failed'"
        args=[time.time(),notification_id]
        if user_id is not None:
            q+=' AND user_id=?'
            args.append(user_id)
        c.execute(q,args)
        return c.rowcount>0


def save_part(notification_id, count, message_id=None):
    """Persist accepted DM parts before attempting the next one."""
    with _connect(commit=True) as c:
        c.execute("UPDATE notification_outbox SET sent_parts=?,discord_message_id=? WHERE id=? AND state='sending'", (count, message_id, notification_id))


def freeze_body(notification_id, body):
    with _connect(commit=True) as c:
        c.execute("UPDATE notification_outbox SET delivery_body=COALESCE(delivery_body,?) WHERE id=? AND state='sending'", (body, notification_id))
        return c.execute('SELECT delivery_body FROM notification_outbox WHERE id=?', (notification_id,)).fetchone()[0]
