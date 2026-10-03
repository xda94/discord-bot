"""Strict model proposals converted into the same deterministic action types."""
from __future__ import annotations
import math
from natural_commands import (
    TrackURLAction, ShowWishlistAction, ShowFlightsAction, DeleteWishlistAction,
    WishlistTargetAction, ClearWishlistTargetAction, WishlistRestockAction,
    RefreshWishlistAction, WishlistGraphAction, ReminderAction,
    CalendarReminderAction, ReminderManageAction, NaturalClarification,
)
from command_time import CURRENCY_ALIASES, duration

INTENTS = {
 'track': {'reference'}, 'wishlist': {'currency'}, 'flights': set(),
 'delete_item': {'reference'}, 'target': {'reference','price','currency'},
 'clear_target': {'reference'}, 'restock': {'reference','enabled'},
 'refresh': {'reference'}, 'graph': {'reference','currency','days','percentage'},
 'reminder': {'when','text','recurrence'},
 'reminder_list': set(), 'reminder_cancel': {'reminder_id'},
 'reminder_snooze': {'reminder_id'}, 'reminder_edit': {'reminder_id','when','text'},
}


def validate_proposal(data):
    if not isinstance(data,dict) or set(data)!={'intent','slots'} or not isinstance(data['intent'], str) or data['intent'] not in INTENTS or not isinstance(data['slots'],dict):
        raise ValueError('Invalid action proposal')
    intent=data['intent']
    s=data['slots']
    if set(s)-INTENTS[intent]:
        raise ValueError('Unsupported action fields')
    for field in ('reference','when','text'):
        if field in s and s[field] is not None and (not isinstance(s[field],str) or len(s[field])>1900):
            raise ValueError('Invalid text field')
    currency=s.get('currency')
    if currency is not None:
        if not isinstance(currency,str):
            raise ValueError('Invalid currency')
        currency=CURRENCY_ALIASES.get(currency.casefold())
        if currency is None:
            raise ValueError('Invalid currency')
    reference=s.get('reference')
    if intent in ('track','delete_item','target','clear_target','restock') and not reference:
        return NaturalClarification('reference',intent,s,'product')
    if intent=='track':
        return TrackURLAction(reference)
    if intent=='wishlist':
        return ShowWishlistAction(currency)
    if intent=='flights':
        return ShowFlightsAction()
    if intent=='delete_item':
        return DeleteWishlistAction(reference)
    if intent=='target':
        if s.get('price') is None:
            return NaturalClarification('price',intent,s,'price')
        n=s['price']
        if isinstance(n,bool) or not isinstance(n,(int,float)) or not math.isfinite(n) or n<=0:
            raise ValueError('Invalid target price')
        if currency is None:
            return NaturalClarification('currency',intent,s,'currency')
        return WishlistTargetAction(reference,n,currency)
    if intent=='clear_target':
        return ClearWishlistTargetAction(reference)
    if intent=='restock':
        if type(s.get('enabled')) is not bool:
            raise ValueError('Invalid restock flag')
        return WishlistRestockAction(reference,s['enabled'])
    if intent=='refresh':
        return RefreshWishlistAction(reference)
    if intent=='graph':
        days=s.get('days',180)
        pct=s.get('percentage',False)
        if type(days) is not int or not 1<=days<=180 or type(pct) is not bool or (pct and currency):
            raise ValueError('Invalid graph options')
        return WishlistGraphAction(reference,currency,days,pct)
    if intent=='reminder':
        if s.get('recurrence') not in (None,'daily','weekdays','weekly'):
            raise ValueError('Invalid recurrence')
        if not s.get('when'):
            return NaturalClarification('when',intent,s,'time')
        if not s.get('text'):
            return NaturalClarification('text',intent,s,'reminder_text')
        seconds=duration(s['when'].removeprefix('in ').removeprefix('peste '))
        if seconds and not s.get('recurrence'):
            return ReminderAction(seconds,seconds/60,'minutes',s['text'])
        return CalendarReminderAction('+'+str(seconds) if seconds else s['when'],s['text'],s.get('recurrence'))
    if intent=='reminder_list':
        return ReminderManageAction('list')
    rid=s.get('reminder_id')
    if type(rid) is not int or rid<=0:
        raise ValueError('Invalid reminder ID')
    return ReminderManageAction(intent.removeprefix('reminder_'),rid,s.get('when'),s.get('text'))


def authorize_proposal(proposal,text,replied_text=''):
    """Reject proposals with operands absent from the authorizing message."""
    import re
    from i18n import fold,language_for
    from command_time import number
    current=fold(text).strip()
    if not current or current.startswith(('"',"'",'`','“')) or re.match(r"(?:nu (?!mai urmari)|don't |do not |cum |how |what can |ce poti |de ce |why |if |daca )",current):
        raise ValueError('Message does not authorize an action')
    if re.search(r"\b(?:si apoi|and then|apoi|then)\b|\b(?:and|si)\s+(?:track|watch|monitor|urmareste|show|list|arata|afiseaza|remind|aminteste|adu|set|seteaza|notify|anunta|delete|remove|sterge|cancel|anuleaza|refresh|actualizeaza|graph|grafic|compare|compara)\b",current):
        raise ValueError('Multiple actions')
    # Require an action-bearing verb in the current message. A fabricated
    # empty-slot list proposal from ordinary conversation must not execute.
    commands = {
        'track': r'(?:track|watch|monitor|follow|urmareste|monitorizeaza|adauga)',
        'wishlist': r'(?:show|list|display|arata|afiseaza|lista)',
        'flights': r'(?:show|list|display|arata|afiseaza|lista)',
        'delete_item': r'(?:stop|remove|delete|sterge|renunta|nu mai urmari)',
        'target': r'(?:notify|alert|set|anunta|seteaza)',
        'clear_target': r'(?:clear|remove|delete|sterge)',
        'restock': r'(?:notify|alert|set|enable|disable|anunta|seteaza|activeaza|dezactiveaza)',
        'refresh': r'(?:refresh|check|update|verifica|actualizeaza)',
        'graph': r'(?:graph|chart|plot|compare|grafic|compara)',
        'reminder': r'(?:remind|remember|aminteste|adu|pune|seteaza)',
        'reminder_list': r'(?:show|list|display|arata|afiseaza|lista)',
        'reminder_cancel': r'(?:cancel|delete|anuleaza|sterge)',
        'reminder_snooze': r'(?:snooze|postpone|amana)',
        'reminder_edit': r'(?:edit|change|update|modifica|schimba|actualizeaza)',
    }
    prefix = r'^(?:(?:please|te rog)[,:]?\s+|(?:can|could|would) you\s+|(?:poti|ai putea)(?: sa)?\s+)?'
    if not re.match(prefix + commands[proposal['intent']] + r'\b', current):
        raise ValueError('Message does not request this action')
    if re.search(r"\b(?:don't|do not|nu)\s+(?:actually\s+)?(?:save|execute|schedule|track|add|salva|executa|programa|urmari|adauga)\b", current):
        raise ValueError('Request denies the action')
    intent = proposal['intent']
    domains = {
        'wishlist': r'\b(?:wishlist|wish list|dorinte|products|items|produse|produsele)\b',
        'flights': r'\b(?:flights|zboruri|zborurile)\b',
        'reminder_list': r'\b(?:reminders|remindere|reminderele|reminderelor)\b',
    }
    if intent in domains and not re.search(domains[intent], current):
        raise ValueError('Invented action domain')
    if intent in ('delete_item', 'clear_target', 'restock') and re.search(r'\breminder', current):
        raise ValueError('Mismatched action domain')
    slots=proposal['slots']
    if intent in ('graph','refresh') and not slots.get('reference') and not re.search(domains['wishlist'], current):
        raise ValueError('Missing explicit wishlist reference')
    recurrence = slots.get('recurrence')
    recurrence_words = {'daily': r'\b(?:daily|zilnic|in fiecare zi)\b', 'weekdays': r'\b(?:weekdays|in fiecare zi lucratoare)\b', 'weekly': r'\b(?:weekly|saptamanal)\b'}
    if recurrence and not re.search(recurrence_words[recurrence], current):
        raise ValueError('Invented recurrence')
    if slots.get('currency') is not None:
        currencies = [CURRENCY_ALIASES.get(word) for word in re.findall(r'\b\w+\b', current)]
        if CURRENCY_ALIASES.get(slots['currency'].casefold()) not in currencies:
            raise ValueError('Invented currency')
    if slots.get('days') is not None and not re.search(r'(?<!\d)' + str(slots['days']) + r'\s+(?:days?|zile|zi)\b', current):
        raise ValueError('Invented graph period')
    if slots.get('percentage') and not re.search(r'\b(?:compare|compara|percentage|percent|procent)\b|%', current):
        raise ValueError('Invented percentage mode')
    if intent == 'restock':
        if not re.search(r'\b(?:restock|stock|stoc)\b', current):
            raise ValueError('Missing restock request')
        negative = bool(re.search(r'\b(?:disable|dezactiveaza)\b', current))
        if slots.get('enabled') == negative:
            raise ValueError('Mismatched restock setting')
    reference=slots.get('reference')
    if reference and fold(reference) not in current:
        if not (proposal['intent']=='track' and reference in replied_text and len(re.findall(r'https?://[^\s<>]+', replied_text)) == 1 and re.search(r'\b(?:this|asta|produsul|acest)\b',current)):
            raise ValueError('Invented product reference')
    for field in ('when','text'):
        if slots.get(field) and fold(slots[field]) not in current:
            raise ValueError('Invented reminder content')
    when = slots.get('when')
    if when and duration(when):
        match = re.search(re.escape(fold(when)), current)
        before, after = current[:match.start()], current[match.end():]
        if re.search(r'\b(?:and|si)\s*$', before) or re.match(r'\s*(?:and|si|,)\s+(?:\d|one|two|three|thirty|half|a quarter|o |un |doua|trei|treizeci|jumatate)', after):
            raise ValueError('Partially consumed duration')
    if slots.get('reminder_id') is not None and not re.search(r'(?<!\d)'+str(slots['reminder_id'])+r'(?!\d)',current):
        raise ValueError('Invented reminder ID')
    if slots.get('price') is not None:
        nums=[number(token,language_for(text)) for token in re.findall(r'\b[\d.,]+\b',current)]
        if slots['price'] not in nums:
            raise ValueError('Invented amount')


def action_candidate(text):
    """Bounded telemetry hint; never authorizes or executes anything."""
    import re
    from i18n import fold
    current = fold(text).strip()
    return bool(re.match(r"^(?:(?:please|te rog)[,:]?\s+|(?:can|could|would) you\s+|(?:poti|ai putea)(?: sa)?\s+)?(?:track|watch|monitor|follow|urmareste|monitorizeaza|show|list|display|arata|afiseaza|refresh|check|update|actualizeaza|verifica|graph|chart|compare|grafic|compara|set|seteaza|clear|enable|disable|activeaza|dezactiveaza|remind|remember|aminteste|adu|pune|notify|alert|anunta|cancel|delete|remove|anuleaza|sterge|snooze|postpone|amana|edit|change|modifica|schimba)\b", current))
