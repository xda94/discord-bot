"""Bounded bilingual time and number parsing, independent of LLM output."""
from __future__ import annotations
import math
import time
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from i18n import fold

WORDS = {'a':1, 'an':1, 'one':1, 'un':1, 'unu':1, 'o':1, 'doua':2, 'doi':2, 'two':2, 'trei':3, 'three':3, 'patru':4, 'four':4, 'cinci':5, 'five':5, 'sase':6, 'six':6, 'sapte':7, 'seven':7, 'opt':8, 'eight':8, 'noua':9, 'nine':9, 'zece':10, 'ten':10, 'jumatate':.5,'fifteen':15,'cincisprezece':15,'twenty':20,'douazeci':20,'thirty':30,'treizeci':30}
UNITS = {'m':60, 'min':60, 'mins':60, 'minute':60, 'minutes':60, 'minut':60, 'h':3600, 'hr':3600, 'hrs':3600, 'hour':3600, 'hours':3600, 'ora':3600, 'ore':3600, 'd':86400, 'day':86400, 'days':86400, 'zi':86400, 'zile':86400, 'w':604800, 'week':604800, 'weeks':604800, 'saptamana':604800, 'saptamani':604800}
CURRENCY_ALIASES = {'lei':'RON', 'leu':'RON', 'ron':'RON', 'euro':'EUR', 'euros':'EUR', 'eur':'EUR', 'usd':'USD', 'dollars':'USD', 'dolari':'USD', 'gbp':'GBP', 'pounds':'GBP', 'lire':'GBP', 'dkk':'DKK'}


def number(value: str, language='en') -> float | None:
    value = fold(value).strip()
    if value in WORDS:
        return float(WORDS[value])
    if not re.fullmatch(r'[+-]?\d+(?:[.,]\d+)*', value):
        return None
    decimal, grouping = (',', '.') if language == 'ro' else ('.', ',')
    if grouping in value:
        # One separator with three trailing digits could mean 1.500 or 1,500.
        if decimal not in value and value.count(grouping) == 1:
            if len(value.split(grouping)[1]) == 3:
                return None
            value = value.replace(grouping, '.')
        else:
            whole = value.split(decimal)[0]
            if not re.fullmatch(r'[+-]?\d{1,3}(?:' + re.escape(grouping) + r'\d{3})+', whole):
                return None
            value = value.replace(grouping, '')
    try:
        n = float(value.replace(decimal, '.'))
        return n if math.isfinite(n) else None
    except ValueError:
        return None


def duration(value: str) -> int | None:
    value = fold(value).strip()
    value = re.sub(r'^(?:in |peste |after |dupa )', '', value)
    value = re.sub(r'^intr-o ', 'o ', value)
    value = re.sub(r'^intr-un ', 'un ', value)
    value = re.sub(r'\b(?:un sfert|a quarter)\s+(?:de |of an? )?', '0.25 ', value)
    value = re.sub(r'\bjumatate\s+de\s+', '0.5 ', value)
    value = re.sub(r'\bhalf\s+(?:an?\s+)?', '0.5 ', value)
    parts = re.split(r'\s+(?:si|and)\s+|\s*,\s*(?=\D)', value)
    seconds = 0.0
    for part in parts:
        m = re.fullmatch(r'([\w.,]+)\s*(?:de\s+)?([a-z]+)', part.strip())
        if not m:
            return None
        n = number(m[1], 'ro' if ',' in m[1] else 'en')
        unit = UNITS.get(m[2])
        if n is None or unit is None or n <= 0:
            return None
        seconds += n * unit
    return int(seconds) if 1 <= seconds <= 10 * 365 * 86400 else None


WEEKDAYS = {'luni':0,'monday':0,'marti':1,'tuesday':1,'miercuri':2,'wednesday':2,'joi':3,'thursday':3,'vineri':4,'friday':4,'sambata':5,'saturday':5,'duminica':6,'sunday':6}


def valid_local(naive: datetime, zone: ZoneInfo) -> list[datetime]:
    choices = []
    for bit in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=bit)
        if aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == naive:
            if all(aware.timestamp() != old.timestamp() for old in choices):
                choices.append(aware)
    return choices


def calendar_time(value: str, zone_name: str, *, now: float | None = None) -> float | None:
    zone = ZoneInfo(zone_name)
    current = datetime.fromtimestamp(now, zone) if now is not None else datetime.now(zone)
    value = fold(value).strip()
    value = re.sub(r'\bnoon$', '12:00', value)
    value = re.sub(r'\b(?:midnight|miezul noptii)$', '00:00', value)
    m = re.fullmatch(r'(.*?)\s*(?:la(?: ora)?|at)?\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', value)
    if not m or not m[1].strip():
        return None
    day_text = m[1].strip()
    h, minute = int(m[2]), int(m[3] or 0)
    if m[4]:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if m[4]=='pm' else 0)
    if not 0 <= h <= 23 or not 0 <= minute <= 59:
        return None
    day = current.date()
    if day_text in ('maine','tomorrow'):
        day += timedelta(days=1)
    elif day_text in WEEKDAYS:
        day += timedelta(days=(WEEKDAYS[day_text] - day.weekday()) % 7)
    elif day_text not in ('azi','astazi','today'):
        try:
            if re.fullmatch(r'\d{1,2}\.\d{1,2}(?:\.\d{4})?', day_text):
                day = datetime.strptime(day_text if day_text.count('.') == 2 else day_text + f'.{current.year}', '%d.%m.%Y').date()
            else:
                day = datetime.strptime(day_text, '%Y-%m-%d').date()
        except ValueError:
            return None
    naive = datetime.combine(day, datetime.min.time()).replace(hour=h, minute=minute)
    choices = valid_local(naive, zone)
    if len(choices) != 1 or choices[0].timestamp() <= current.timestamp():
        return None
    return choices[0].timestamp()


def next_occurrence(previous: float, recurrence: str, zone_name: str, now: float) -> float:
    zone = ZoneInfo(zone_name)
    base = datetime.fromtimestamp(previous, zone)
    step = 7 if recurrence == 'weekly' else 1
    elapsed_days = (datetime.fromtimestamp(now, zone).date() - base.date()).days
    day = base.date() + timedelta(days=max(0, elapsed_days // step - 1) * step)
    while True:
        day += timedelta(days=step)
        if recurrence == 'weekdays' and day.weekday() > 4:
            continue
        naive = datetime.combine(day, base.time().replace(tzinfo=None))
        choices = valid_local(naive, zone)
        if choices and choices[0].timestamp() > now:
            return choices[0].timestamp()


def reminder_time(value: str, zone_name: str, *, recurrence=None, now=None):
    """Resolve a calendar time or the next recurring local clock time."""
    now=time.time() if now is None else now
    if value.startswith('+') and value[1:].isdigit():
        seconds=int(value[1:])
        return now+seconds if 0<seconds<=10*365*86400 else None
    clock=re.fullmatch(r'(?:la |at )?(\d{1,2})(?::(\d{2}))?',fold(value))
    if not recurrence or not clock:
        return calendar_time(value,zone_name,now=now)
    h,m=int(clock[1]),int(clock[2] or 0)
    if h>23 or m>59:
        return None
    zone=ZoneInfo(zone_name)
    current=datetime.fromtimestamp(now,zone)
    for offset in range(8):
        day=current.date()+timedelta(days=offset)
        if recurrence=='weekdays' and day.weekday()>4:
            continue
        naive=datetime.combine(day,datetime.min.time()).replace(hour=h,minute=m)
        choices=valid_local(naive,zone)
        if len(choices) != 1 and naive > current.replace(tzinfo=None):
            return None
        if len(choices)==1 and choices[0].timestamp()>now:
            return choices[0].timestamp()
    return None
