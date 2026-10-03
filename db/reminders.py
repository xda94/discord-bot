"""Reminder lifecycle and occurrence claims shared by Discord and the API."""
from __future__ import annotations
import math
import time
from db.connection import _connect
from command_time import next_occurrence

_COLUMNS = 'id,user_id,channel_id,remind_at,message,creator_id,timezone,language,recurrence,state,attempt_count,next_attempt,last_error,claimed_at,finished_at,discord_message_id,revision'.split(',')


def _row(row):
    return dict(zip(_COLUMNS,row)) if row else None


def create(user_id, channel_id, remind_at, message, *, creator_id=None, timezone='UTC', language='en', recurrence=None):
    if not isinstance(message,str) or not message.strip() or len(message)>1900:
        raise ValueError('Reminder text must contain 1–1900 characters')
    if isinstance(remind_at,bool) or not isinstance(remind_at,(float,int)) or not math.isfinite(remind_at) or remind_at <= time.time():
        raise ValueError('Reminder time must be in the future')
    if recurrence not in (None,'daily','weekdays','weekly') or language not in ('en','ro'):
        raise ValueError('Invalid recurrence or language')
    from zoneinfo import ZoneInfo
    ZoneInfo(timezone)
    with _connect(commit=True) as c:
        c.execute('INSERT INTO reminders(user_id,channel_id,remind_at,message,creator_id,timezone,language,recurrence) VALUES(?,?,?,?,?,?,?,?)',
                  (user_id,channel_id,remind_at,message,creator_id or user_id,timezone,language,recurrence))
        return c.lastrowid


def get(reminder_id, user_id=None):
    with _connect() as c:
        query = 'SELECT '+','.join(_COLUMNS)+' FROM reminders WHERE id=?'
        args = [reminder_id]
        if user_id is not None:
            query += ' AND (user_id=? OR creator_id=?)'
            args += [user_id,user_id]
        return _row(c.execute(query,args).fetchone())


def list_for(user_id=None):
    with _connect() as c:
        query='SELECT '+','.join(_COLUMNS)+' FROM reminders'
        args=[]
        if user_id is not None:
            query+=' WHERE user_id=? OR creator_id=?'
            args=[user_id,user_id]
        return [_row(r) for r in c.execute(query+' ORDER BY remind_at,id',args).fetchall()]


def edit(reminder_id, user_id=None, **updates):
    if not updates or set(updates)-{'remind_at','message','recurrence','timezone'}:
        raise ValueError('Unsupported reminder update')
    old=get(reminder_id,user_id)
    if old is None or old['state'] in ('sending','cancelled','delivered'):
        return False
    merged={**old,**updates}
    if not isinstance(merged['message'],str) or not 1<=len(merged['message'].strip())<=1900:
        raise ValueError('Invalid reminder message')
    if not isinstance(merged['remind_at'],(int,float)) or isinstance(merged['remind_at'],bool) or not math.isfinite(merged['remind_at']) or merged['remind_at']<=time.time():
        raise ValueError('Reminder time must be in the future')
    if merged['recurrence'] not in (None,'daily','weekdays','weekly'):
        raise ValueError('Invalid recurrence')
    from zoneinfo import ZoneInfo
    ZoneInfo(merged['timezone'])
    with _connect(commit=True) as c:
        assignments=','.join(k+'=?' for k in updates)
        c.execute('UPDATE reminders SET '+assignments+",state='pending',attempt_count=0,next_attempt=NULL,last_error=NULL,revision=revision+1 WHERE id=? AND revision=? AND state NOT IN ('sending','cancelled','delivered')",(*updates.values(),reminder_id,old['revision']))
        return c.rowcount>0


def cancel(reminder_id,user_id=None):
    old=get(reminder_id,user_id)
    if not old:
        return False
    with _connect(commit=True) as c:
        c.execute("UPDATE reminders SET state='cancelled',finished_at=?,revision=revision+1 WHERE id=? AND revision=? AND state != 'sending'",(time.time(),reminder_id,old['revision']))
        return c.rowcount>0


def retry(reminder_id,user_id=None):
    old=get(reminder_id,user_id)
    if not old or old['state']!='failed':
        return False
    with _connect(commit=True) as c:
        c.execute("UPDATE reminders SET state='pending',attempt_count=0,next_attempt=?,last_error=NULL,finished_at=NULL,revision=revision+1 WHERE id=? AND state='failed'",(time.time(),reminder_id))
        return c.rowcount>0


def snooze(reminder_id,user_id=None,seconds=600):
    old=get(reminder_id,user_id)
    if not old or seconds<=0:
        return None
    return create(old['user_id'],old['channel_id'],time.time()+seconds,old['message'],creator_id=old['creator_id'],timezone=old['timezone'],language=old['language'])


def claim_due(now=None):
    now=time.time() if now is None else now
    with _connect(commit=True) as c:
        c.execute('BEGIN IMMEDIATE')
        # A stale claim is uncertain, not automatically retried. The bot attempts
        # reconciliation before an operator can explicitly retry it.
        c.execute("UPDATE reminders SET state='failed',last_error='uncertain-delivery',finished_at=? WHERE state='sending' AND claimed_at<?",(now,now-300))
        rows=[_row(r) for r in c.execute('SELECT '+','.join(_COLUMNS)+" FROM reminders WHERE state='pending' AND remind_at<=? AND COALESCE(next_attempt,remind_at)<=? ORDER BY remind_at LIMIT 50",(now,now)).fetchall()]
        for row in rows:
            c.execute("UPDATE reminders SET state='sending',claimed_at=? WHERE id=? AND state='pending'",(now,row['id']))
            c.execute("INSERT INTO reminder_occurrences(reminder_id,scheduled_at,state) VALUES(?,?,'sending') ON CONFLICT(reminder_id,scheduled_at) DO UPDATE SET state='sending'",(row['id'],row['remind_at']))
        return rows


def complete(row,message_id=None,now=None):
    now=time.time() if now is None else now
    with _connect(commit=True) as c:
        c.execute("UPDATE reminder_occurrences SET state='delivered',discord_message_id=? WHERE reminder_id=? AND scheduled_at=?",(message_id,row['id'],row['remind_at']))
        if row['recurrence']:
            following=next_occurrence(row['remind_at'],row['recurrence'],row['timezone'],now)
            c.execute("UPDATE reminders SET state='pending',remind_at=?,attempt_count=0,next_attempt=NULL,claimed_at=NULL,last_error=NULL,discord_message_id=? WHERE id=? AND state IN ('sending','failed')",(following,message_id,row['id']))
        else:
            c.execute("UPDATE reminders SET state='delivered',finished_at=?,discord_message_id=? WHERE id=? AND state IN ('sending','failed')",(now,message_id,row['id']))


def fail(row,error,permanent=False,now=None):
    now=time.time() if now is None else now
    attempt=row['attempt_count']+1
    delays=(60,300,900)
    terminal=permanent or attempt>len(delays)
    with _connect(commit=True) as c:
        c.execute('UPDATE reminders SET state=?,attempt_count=?,next_attempt=?,last_error=?,finished_at=? WHERE id=? AND state=\'sending\'',
                  ('failed' if terminal else 'pending',attempt,None if terminal else now+delays[attempt-1],error[:200],now if terminal else None,row['id']))
        c.execute('UPDATE reminder_occurrences SET state=?,last_error=? WHERE reminder_id=? AND scheduled_at=?',('failed' if terminal else 'pending',error[:200],row['id'],row['remind_at']))


def cleanup(now=None):
    now=time.time() if now is None else now
    with _connect(commit=True) as c:
        c.execute('DELETE FROM reminder_occurrences WHERE reminder_id IN (SELECT id FROM reminders WHERE finished_at<?)',(now-30*86400,))
        c.execute("DELETE FROM reminder_occurrences WHERE state='delivered' AND scheduled_at<?",(now-30*86400,))
        c.execute('DELETE FROM reminders WHERE finished_at<?',(now-30*86400,))
        c.execute('DELETE FROM action_executions WHERE created_at<?',(now-30*86400,))


def claim_action(event_id):
    with _connect(commit=True) as c:
        c.execute("INSERT OR IGNORE INTO action_executions(event_id,state,created_at) VALUES(?,'claimed',?)",(str(event_id),time.time()))
        return c.rowcount>0


def finish_action(event_id,success):
    with _connect(commit=True) as c:
        c.execute('UPDATE action_executions SET state=? WHERE event_id=?',('executed' if success else 'failed',str(event_id)))
