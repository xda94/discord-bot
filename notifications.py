"""Scheduling and delivery shared by price and flight tracking."""
from __future__ import annotations
import time
import json
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import discord
from discord.ext import tasks
import db
from db import notifications as store
from assistant_profiles import effective_profile
from command_time import valid_local
from i18n import t,language_for,localize
from analytics import record


def delivery_at(profile,now=None):
    now=time.time() if now is None else now
    if not profile.timezone_configured:
        return now
    zone=ZoneInfo(profile.timezone)
    local=datetime.fromtimestamp(now,zone)
    result=local
    if profile.delivery_mode=='daily':
        h,m=map(int,profile.digest_time.split(':'))
        result=local.replace(hour=h,minute=m,second=0,microsecond=0)
        if result.timestamp()<=now:
            result+=timedelta(days=1)
    if profile.quiet_start and profile.quiet_end:
        current=result.strftime('%H:%M')
        start=profile.quiet_start
        end=profile.quiet_end
        quiet=(start<=current<end) if start<end else (current>=start or current<end)
        if quiet:
            h,m=map(int,end.split(':'))
            if start>end and current>=start:
                result+=timedelta(days=1)
            result=result.replace(hour=h,minute=m,second=0,microsecond=0)
    # Move a nonexistent spring-forward time to the first valid minute.
    naive=result.replace(tzinfo=None)
    for _ in range(181):
        candidates=valid_local(naive,zone)
        if candidates:
            return max(now,candidates[0].timestamp())
        naive+=timedelta(minutes=1)
    return now+3600


def tracking_language(user_id, source, item_key):
    from db.connection import _connect
    table = {'wishlist':'scraped_items','flight':'flight_trackers'}.get(source)
    captured = 'en'
    if table:
        with _connect() as c:
            row = c.execute(f'SELECT language FROM {table} WHERE user_id=? AND id=?', (user_id,item_key)).fetchone()
            if row:
                captured = row[0]
    return language_for(profile=db.get_assistant_profile(user_id),fallback=captured)


async def deliver_tracking(client, user_id, source, item_key, event_kind, body, *, protected=()):
    profile = effective_profile(db.get_assistant_profile(user_id))
    lang = tracking_language(user_id,source,item_key)
    store.enqueue(user_id, source, item_key, event_kind,
                  body, lang, delivery_at(profile), protected=protected)
    await drain(client, user_id=user_id)


async def drain(client, user_id=None):
    groups = {}
    for row in store.claim(user_id=user_id):
        groups.setdefault(row['user_id'], []).append(row)
    for recipient, events in groups.items():
        remaining = list(events)
        try:
            profile = effective_profile(db.get_assistant_profile(recipient))
            lang = language_for(profile=profile, fallback=events[0]['language'])
            if profile.quiet_start and profile.delivery_mode == 'immediate':
                due = delivery_at(profile)
                if due > time.time() + 1:
                    with store._connect(commit=True) as c:
                        for event in events:
                            c.execute("UPDATE notification_outbox SET state='pending',due_at=? WHERE id=?", (due, event['id']))
                    continue
            user = await client.fetch_user(recipient)
            if user is None:
                raise RuntimeError('User unavailable')
            # Commit each accepted event/part separately. A later DM failure
            # must not cause previously delivered events to be sent again.
            for event in events:
                lang = language_for(profile=profile, fallback=event['language'])
                observed = datetime.fromtimestamp(event['observed_at'], ZoneInfo(profile.timezone)).isoformat(timespec='minutes')
                header = t('digest', lang) + '\n\n' if profile.delivery_mode == 'daily' else ''
                body = header + localize(event['body'], lang, protected=json.loads(event['protected_json'])) + '\n' + t('observed', lang, time=observed)
                body = store.freeze_body(event['id'], body)
                parts = [body[offset:offset + 1900] for offset in range(0, len(body), 1900)]
                for index in range(event['sent_parts'], len(parts)):
                    sent = await user.send(parts[index], suppress_embeds=True, allowed_mentions=discord.AllowedMentions.none())
                    message_id = getattr(sent, 'id', None)
                    store.save_part(event['id'], index + 1, message_id if isinstance(message_id, int) else None)
                store.finish([event])
                remaining.remove(event)
                await record('scheduled', event['source'] + '-notification', scope_type='dm')
        except (discord.Forbidden, discord.NotFound) as exc:
            store.finish(remaining, type(exc).__name__, permanent=True)
            for event in remaining:
                await record('failure', event['source'] + '-notification', scope_type='dm')
        except Exception as exc:
            uncertain = isinstance(exc, (TimeoutError, ConnectionError))
            store.finish(remaining, 'uncertain-delivery' if uncertain else type(exc).__name__, permanent=uncertain)
            for event in remaining:
                await record('failure', event['source'] + '-notification', scope_type='dm')


class NotificationsFeature:
    def __init__(self,client):
        self.client=client
    async def start_tasks(self):
        if not self._check.is_running():
            self._check.start()
    @tasks.loop(seconds=30)
    async def _check(self):
        await drain(self.client)
