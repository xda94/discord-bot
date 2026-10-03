"""Anchored, deterministic parsing for the bot's bounded natural commands."""

from __future__ import annotations

import re
import math
from dataclasses import dataclass
from typing import Union


_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "unu": 1, "un": 1, "doi": 2, "doua": 2, "două": 2, "trei": 3,
    "patru": 4, "cinci": 5, "sase": 6, "șase": 6, "sapte": 7,
    "șapte": 7, "opt": 8, "noua": 9, "nouă": 9, "zece": 10,
}
_UNIT_SECONDS = {
    "minute": 60, "minutes": 60, "minut": 60, "minute": 60,
    "min": 60, "mins": 60,
    "hour": 3600, "hours": 3600, "ora": 3600, "oră": 3600,
    "ore": 3600, "hr": 3600, "hrs": 3600,
    "day": 86400, "days": 86400, "zi": 86400, "zile": 86400,
}


@dataclass(frozen=True)
class TrackURLAction:
    url: str


@dataclass(frozen=True)
class ShowFlightsAction:
    pass


@dataclass(frozen=True)
class ReminderAction:
    seconds: int
    amount: float
    unit: str
    text: str


@dataclass(frozen=True)
class ShowWishlistAction:
    currency: str | None = None


@dataclass(frozen=True)
class DeleteWishlistAction:
    url: str


@dataclass(frozen=True)
class WishlistTargetAction:
    url: str
    price: float
    currency: str


@dataclass(frozen=True)
class ClearWishlistTargetAction:
    url: str


@dataclass(frozen=True)
class WishlistRestockAction:
    url: str
    enabled: bool


@dataclass(frozen=True)
class RefreshWishlistAction:
    url: str | None = None


@dataclass(frozen=True)
class WishlistGraphAction:
    url: str | None = None
    currency: str | None = None
    days: int = 180
    percentage: bool = False


@dataclass(frozen=True)
class NaturalCommandError:
    message: str
    key: str = "invalid"

    def __post_init__(self):
        if self.key != 'invalid':
            return
        text = self.message.casefold()
        key = 'currency' if 'currenc' in text else 'duration' if 'duration' in text else 'number' if 'price must' in text else 'multiple' if 'one action' in text else 'invalid'
        object.__setattr__(self, 'key', key)


NaturalAction = Union[
    TrackURLAction,
    ShowFlightsAction,
    ReminderAction,
    ShowWishlistAction,
    DeleteWishlistAction,
    WishlistTargetAction,
    ClearWishlistTargetAction,
    WishlistRestockAction,
    RefreshWishlistAction,
    WishlistGraphAction,
    "CalendarReminderAction",
    "ReminderManageAction",
]

NaturalRequestResult = Union[NaturalAction, NaturalCommandError, "NaturalClarification", None]

_SUPPORTED_CURRENCIES = frozenset({"RON", "DKK", "EUR", "USD", "GBP"})


def _urls(value: str) -> list[str]:
    return [match.group(0).rstrip(".,;:!?)]}") for match in _URL_RE.finditer(value or "")]


def _number(value: str) -> float | None:
    normalized = value.casefold()
    if normalized in _NUMBER_WORDS:
        return float(_NUMBER_WORDS[normalized])
    try:
        parsed = float(normalized.replace(",", "."))
        return parsed if math.isfinite(parsed) else None
    except ValueError:
        return None


def _currency(value: str) -> str | None:
    normalized = value.upper()
    return normalized if normalized in _SUPPORTED_CURRENCIES else None


def _single_url_tail(tail: str, *, usage: str) -> str | NaturalCommandError:
    direct_urls = _urls(tail)
    if len(direct_urls) > 1:
        return NaturalCommandError("Please provide exactly one HTTP(S) product URL.")
    if len(direct_urls) == 0:
        return NaturalCommandError(usage)
    if tail.strip() != direct_urls[0]:
        return NaturalCommandError("Use one URL and no extra instructions.")
    return direct_urls[0]


def _graph_options(
    remainder: str, *, allow_currency: bool = True
) -> tuple[str | None, int, NaturalCommandError | None]:
    """Parse the bounded, ordered graph option suffix."""
    rest = remainder.strip()
    if not rest:
        return None, 180, None
    currency = None
    days = 180
    currency_match = re.match(r"(?:in|în)\s+(\S+)(.*)$", rest, re.I)
    if currency_match:
        if not allow_currency:
            return None, 180, NaturalCommandError("Percentage comparison does not accept a currency.")
        currency = _currency(currency_match.group(1))
        if currency is None:
            return None, 180, NaturalCommandError(
                "Use one of these currencies: RON, DKK, EUR, USD, or GBP."
            )
        rest = currency_match.group(2).strip()
    days_match = re.fullmatch(
        r"(?:for|pentru)\s+(\d+)\s+(?:days?|zile?)", rest, re.I
    )
    if rest and not days_match:
        return None, 180, NaturalCommandError(
            "Use a graph period from 1 to 180 whole days."
        )
    if days_match:
        days = int(days_match.group(1))
        if not 1 <= days <= 180:
            return None, 180, NaturalCommandError(
                "Use a graph period from 1 to 180 whole days."
            )
    return currency, days, None


def _parse_legacy_command(
    text: str, *, replied_text: str = ""
) -> NaturalAction | NaturalCommandError | None:
    """Parse only explicitly supported full-message phrases.

    Unsupported prose returns ``None`` so the normal mention LLM can handle it.
    Recognized but incomplete/ambiguous commands return actionable guidance.
    """
    value = (text or "").strip()
    if not value:
        return None

    # Wishlist reads and mutations intentionally use a small collection of
    # complete, deterministic phrases.  Options are parsed in a fixed order;
    # free-form prose never reaches a helper by accident.
    show_wishlist = re.fullmatch(
        r"(?:show\s+my\s+wishlist|arat[aă]-mi\s+wishlist-ul)"
        r"(?:\s+(?:in|în)\s+(\S+))?",
        value,
        flags=re.IGNORECASE,
    )
    if show_wishlist:
        currency_text = show_wishlist.group(1)
        if currency_text is None:
            return ShowWishlistAction()
        currency = _currency(currency_text)
        if currency is None:
            return NaturalCommandError(
                "Use one of these currencies: RON, DKK, EUR, USD, or GBP."
            )
        return ShowWishlistAction(currency)

    if re.match(r"(?:show\s+my\s+wishlist|arat[aă]-mi\s+wishlist-ul)\b", value, re.I):
        return NaturalCommandError("Use `show my wishlist [in EUR]`.")

    delete_match = re.fullmatch(
        r"(?:stop\s+tracking|nu\s+mai\s+urm[aă]ri)\s*(.*)",
        value,
        flags=re.IGNORECASE,
    )
    if delete_match:
        url = _single_url_tail(
            delete_match.group(1),
            usage="Include one HTTP(S) URL to stop tracking.",
        )
        return url if isinstance(url, NaturalCommandError) else DeleteWishlistAction(url)

    if re.match(r"(?:stop\s+tracking|nu\s+mai\s+urm[aă]ri)\b", value, re.I):
        return NaturalCommandError("Use `stop tracking <URL>`." )

    target_match = re.fullmatch(
        r"(?:set\s+target\s+price\s+for|seteaz[aă]\s+pre[tț]ul\s+[tț]int[aă]\s+pentru)\s+(.+?)\s+"
        r"(?:to|la)\s+(\S+)\s+(\S+)",
        value,
        flags=re.IGNORECASE,
    )
    if target_match:
        url = _single_url_tail(
            target_match.group(1),
            usage="Include one HTTP(S) URL for the target price.",
        )
        if isinstance(url, NaturalCommandError):
            return url
        amount = _number(target_match.group(2))
        currency = _currency(target_match.group(3))
        if amount is None or not math.isfinite(amount) or amount <= 0:
            return NaturalCommandError("Target price must be a finite number greater than zero.")
        if currency is None:
            return NaturalCommandError(
                "Use one of these currencies: RON, DKK, EUR, USD, or GBP."
            )
        return WishlistTargetAction(url, amount, currency)

    if re.match(
        r"(?:set\s+target\s+price\s+for|seteaz[aă]\s+pre[tț]ul\s+[tț]int[aă]\s+pentru)\b",
        value,
        re.I,
    ):
        return NaturalCommandError(
            "Use `set target price for <URL> to 100 EUR`."
        )

    clear_target_match = re.fullmatch(
        r"(?:clear\s+target\s+price\s+for|[sș]terge\s+pre[tț]ul\s+[tț]int[aă]\s+pentru)\s*(.*)",
        value,
        flags=re.IGNORECASE,
    )
    if clear_target_match:
        url = _single_url_tail(
            clear_target_match.group(1),
            usage="Include one HTTP(S) URL to clear its target price.",
        )
        return url if isinstance(url, NaturalCommandError) else ClearWishlistTargetAction(url)

    if re.match(
        r"(?:clear\s+target\s+price\s+for|[sș]terge\s+pre[tț]ul\s+[tț]int[aă]\s+pentru)\b",
        value,
        re.I,
    ):
        return NaturalCommandError("Use `clear target price for <URL>`." )

    restock_match = re.fullmatch(
        r"(enable|disable)\s+restock\s+only\s+for\s+(.*)|"
        r"(activeaz[aă]|dezactiveaz[aă])\s+doar\s+notific(?:ările|arile)\s+de\s+stoc\s+pentru\s+(.*)",
        value,
        flags=re.IGNORECASE,
    )
    if restock_match:
        english_enabled, english_tail, romanian_enabled, romanian_tail = restock_match.groups()
        enabled = (
            english_enabled.casefold() == "enable"
            if english_enabled is not None
            else romanian_enabled.casefold().startswith("activeaz")
        )
        tail = english_tail if english_tail is not None else romanian_tail
        url = _single_url_tail(tail, usage="Include one HTTP(S) URL for restock-only mode.")
        return url if isinstance(url, NaturalCommandError) else WishlistRestockAction(url, enabled)

    if re.match(
        r"(?:enable|disable)\s+restock\s+only\b|(?:activeaz[aă]|dezactiveaz[aă])\s+doar\s+notific(?:ările|arile)",
        value,
        re.I,
    ):
        return NaturalCommandError("Use `enable restock only for <URL>`." )

    refresh_all = re.fullmatch(
        r"refresh\s+my\s+wishlist|actualizeaz[aă]\s+wishlist-ul\s+meu",
        value,
        flags=re.IGNORECASE,
    )
    if refresh_all:
        return RefreshWishlistAction()
    refresh_one = re.fullmatch(
        r"(?:refresh|actualizeaz[aă])\s+(.*)",
        value,
        flags=re.IGNORECASE,
    )
    if refresh_one:
        url = _single_url_tail(refresh_one.group(1), usage="Include one HTTP(S) URL to refresh.")
        return url if isinstance(url, NaturalCommandError) else RefreshWishlistAction(url)
    if re.match(r"(?:refresh|actualizeaz[aă])\b", value, re.I):
        return NaturalCommandError("Use `refresh <URL>` or `refresh my wishlist`." )

    # Graphs have a URL or one of the explicit "my wishlist" targets followed
    # by an optional currency and then an optional whole-day period.
    graph_all = re.fullmatch(
        r"graph\s+my\s+wishlist(.*)|grafic\s+pentru\s+wishlist-ul\s+meu(.*)",
        value,
        flags=re.IGNORECASE,
    )
    if graph_all:
        remainder = graph_all.group(1) if graph_all.group(1) is not None else graph_all.group(2)
        currency, days, error = _graph_options(remainder)
        if error:
            return error
        return WishlistGraphAction(currency=currency, days=days)

    compare = re.fullmatch(
        r"compare\s+my\s+wishlist(.*)|compar[aă]\s+wishlist-ul\s+meu(.*)",
        value,
        flags=re.IGNORECASE,
    )
    if compare:
        remainder = compare.group(1) if compare.group(1) is not None else compare.group(2)
        currency, days, error = _graph_options(remainder, allow_currency=False)
        if error:
            return error
        return WishlistGraphAction(days=days, percentage=True)

    graph_url = re.fullmatch(
        r"graph\s+(https?://[^\s<>]+)(.*)|grafic\s+pentru\s+(https?://[^\s<>]+)(.*)",
        value,
        flags=re.IGNORECASE,
    )
    if graph_url:
        url = graph_url.group(1) or graph_url.group(3)
        remainder = graph_url.group(2) if graph_url.group(1) else graph_url.group(4)
        # `_urls` strips terminal punctuation; graph URL matching must retain
        # the same strict no-trailing-instructions rule as mutations.
        if _urls(url) != [url]:
            return NaturalCommandError("Use one URL and no extra instructions.")
        currency, days, error = _graph_options(remainder)
        if error:
            return error
        return WishlistGraphAction(url=url, currency=currency, days=days)

    if re.match(r"(?:graph|grafic|compare|compar[aă])\b", value, re.I):
        return NaturalCommandError(
            "Use `graph <URL> [in EUR] [for 30 days]` or `graph my wishlist`."
        )

    if re.fullmatch(
        r"(?:show|list)(?: me)? my flights|"
        r"(?:arat[aă]|afi[sș]eaz[aă])(?:-mi)? (?:zborurile|zborurile mele)",
        value,
        flags=re.IGNORECASE,
    ):
        return ShowFlightsAction()

    track = re.fullmatch(
        r"(?:please\s+)?(?:track|watch|monitor)(?:\s+(?:this|this item|this product))?(?:\s+(.*))?|"
        r"(?:te rog\s+)?(?:urm[aă]re[sș]te|monitorizeaz[aă])(?:\s+(?:asta|acest produs|produsul))?(?:\s+(.*))?",
        value,
        flags=re.IGNORECASE,
    )
    if track:
        tail = next((group for group in track.groups() if group is not None), "")
        direct_urls = _urls(tail)
        if len(direct_urls) > 1:
            return NaturalCommandError("Please provide exactly one HTTP(S) product URL to track.")
        if len(direct_urls) == 1:
            if tail.strip() != direct_urls[0]:
                return NaturalCommandError("Use only one URL, for example: `track https://example.com/product`." )
            return TrackURLAction(direct_urls[0])
        if tail.strip():
            return NaturalCommandError("Include one HTTP(S) URL, or reply to a message containing exactly one URL.")
        reply_urls = _urls(replied_text)
        if len(reply_urls) == 1:
            return TrackURLAction(reply_urls[0])
        if len(reply_urls) > 1:
            return NaturalCommandError("The replied message has multiple URLs. Reply to a message containing exactly one URL.")
        return NaturalCommandError("Include one HTTP(S) URL, or reply to a message containing exactly one URL.")

    reminder = re.fullmatch(
        r"(?:please\s+)?remind me in\s+(\S+)\s+([\wăâîșţț]+)\s+(?:to\s+)?(.+)|"
        r"(?:te rog\s+)?aminte[sș]te-mi peste\s+(\S+)\s+([\wăâîșţț]+)\s+(?:s[aă]\s+)?(.+)",
        value,
        flags=re.IGNORECASE,
    )
    if reminder:
        groups = reminder.groups()
        amount_text, unit, message = (
            groups[:3] if groups[0] is not None else groups[3:]
        )
        amount = _number(amount_text)
        unit_key = unit.casefold()
        if amount is None or amount <= 0:
            return NaturalCommandError("Use a positive duration, for example: `remind me in 20 minutes to stretch`." )
        if unit_key not in _UNIT_SECONDS:
            return NaturalCommandError("Use minutes, hours, or days for the reminder duration.")
        if not message.strip():
            return NaturalCommandError("Say what to remember after the duration.")
        seconds = int(amount * _UNIT_SECONDS[unit_key])
        if seconds <= 0:
            return NaturalCommandError("The reminder duration is too short.")
        return ReminderAction(seconds, amount, unit, message.strip())

    # Recognized reminder openings get guidance rather than an LLM guess.
    if re.match(r"(?:please\s+)?remind me\b|(?:te rog\s+)?aminte[sș]te-mi\b", value, re.I):
        return NaturalCommandError(
            "Use `remind me in 20 minutes to stretch` or `amintește-mi peste 2 ore să sun acasă`."
        )
    return None


@dataclass(frozen=True)
class CalendarReminderAction:
    when: str
    text: str
    recurrence: str | None = None


@dataclass(frozen=True)
class ReminderManageAction:
    operation: str
    reminder_id: int | None = None
    when: str | None = None
    text: str | None = None


@dataclass(frozen=True)
class NaturalClarification:
    field: str
    intent: str
    slots: dict
    key: str


def _clean_request(text: str) -> str:
    value = (text or "").strip()
    value = re.sub(r"^(?:please|te rog)[,:]?\s+", "", value, flags=re.I)
    value = re.sub(r"[, ]+(?:please|te rog)[.!?]*$", "", value, flags=re.I)
    override = r'(?:respond|reply|answer|raspunde)\s+(?:in|în)\s+(?:english|romanian|engleza|engleză|romana|română)'
    value = re.sub(r'[,; ]+' + override + r'[.!?]*$', '', value, flags=re.I)
    if not re.match(r'(?:remind|aminteste|amintește|adu|pune|seteaza|setează)', value, re.I):
        value = re.sub(r'\s+(?:in|în)\s+(?:english|romanian|engleza|engleză|romana|română)[.!?]*$', '', value, flags=re.I)
    return value.strip()


def parse_natural_command(text: str, *, replied_text: str = "") -> NaturalRequestResult:
    """Fast bilingual recognition. Original text/URLs remain untouched."""
    from i18n import fold, language_for, t
    from command_time import duration, number, CURRENCY_ALIASES
    value = _clean_request(text)
    # Keep a source map while normalizing whitespace for recognition.
    normalized, positions = [], []
    for token in re.finditer(r"\S+", value):
        if normalized:
            normalized.append(' ')
            positions.append(token.start() - 1)
        for offset, character in enumerate(token[0]):
            folded = fold(character)
            normalized.extend(folded)
            positions.extend([token.start() + offset] * len(folded))
    key = ''.join(normalized).rstrip(".!? ")
    def original(start, end=None):
        if end is None:
            return value[positions[start]:]
        return value[positions[start]:positions[end - 1] + 1] if end > start else ''
    lang = language_for(text)
    if not key:
        return None
    if re.match(r"(?:nu (?!mai urmari)|don't |do not |cum |how |ce inseamna |what does |de ce |why )", key) or value.startswith(('"', "'", '“', '`')):
        return None
    action_opening = re.match(r'(?:track|watch|monitor|urmareste|monitorizeaza|show|list|arata|afiseaza|refresh|actualizeaza|graph|grafic|compare|compara|set|seteaza|clear|sterge|enable|disable|activeaza|dezactiveaza|remind|aminteste|adu|pune|anunta|notify|cancel|anuleaza|snooze|amana|edit|modifica)\b', key)
    additional = r'(?:track|urmareste|show|arata|refresh|actualizeaza|set|seteaza|remind|aminteste|adu|anunta|notify|cancel|anuleaza|delete|sterge)'
    if action_opening and (re.search(r"\b(?:si apoi|and then|apoi|then)\b", key) or re.search(r'(?:;\s*|\b(?:and|si)\s+)' + additional + r'\b', key)):
        return NaturalCommandError(t('multiple', lang), 'multiple')

    opening = re.match(r"(?:remind me|aminteste[- ]mi|adu[- ]mi aminte|pune[- ]mi (?:un )?reminder|seteaza (?:un )?reminder)\s+(.*)$", key)
    if opening:
        original_tail = original(opening.start(1))
        tail = opening[1]
        recurrence = None
        recurring = re.match(r"(?:zilnic|daily|in fiecare zi lucratoare|in fiecare zi|weekdays|saptamanal|weekly)\s+", tail)
        if recurring:
            recurrence = ('weekdays' if recurring[0].strip() in ('in fiecare zi lucratoare', 'weekdays') else 'weekly' if recurring[0].strip() in ('saptamanal','weekly') else 'daily')
            tail = tail[recurring.end():]
            original_tail = original(opening.start(1) + recurring.end())
        leading_text = re.match(r'(?:sa|to)\s+', tail)
        if leading_text:
            base = opening.start(1) + (recurring.end() if recurring else 0)
            return NaturalClarification('when','reminder',{'text':original(base + leading_text.end()),'recurrence':recurrence},'time')
        # The explicit to/sa delimiter prevents consuming duration fragments as text.
        split = re.search(r"\s+(?:sa|to)\s+", tail)
        if split:
            when = tail[:split.start()]
            base = opening.start(1) + (recurring.end() if recurring else 0)
            message = original(base + split.end()).strip()
        else:
            # Legacy shorthand supports one duration followed by the message.
            compact = re.match(r"(?:peste|in)\s+([\w.,]+\s+(?:de\s+)?(?:minutes?|minut|ore|ora|hours?|days?|zile|zi|weeks?|saptamani))\s+(.+)", tail)
            if not compact:
                return NaturalClarification('text', 'reminder', {'when': original_tail}, 'reminder_text')
            when = tail[:compact.end(1)]
            base = opening.start(1) + (recurring.end() if recurring else 0)
            message = original(base + compact.start(2))
            if re.match(r"(?:si|and|\d|de)\b", fold(message)):
                return NaturalCommandError(t('duration', lang), 'duration')
        if when.startswith(('peste ', 'in ', 'intr-o ', 'intr-un ', 'dupa ')):
            duration_text = re.sub(r'^(?:peste |in |dupa )', '', when)
            duration_text = re.sub(r'^intr-o ', 'o ', duration_text)
            duration_text = re.sub(r'^intr-un ', 'un ', duration_text)
            seconds = duration(duration_text)
            if seconds is None:
                return NaturalCommandError(t('duration', lang), 'duration')
            if recurrence:
                return CalendarReminderAction('+' + str(seconds), message, recurrence)
            return ReminderAction(seconds, seconds / 60, 'minutes', message)
        return CalendarReminderAction(when, message, recurrence)

    manage = re.fullmatch(r"(?:show|list)(?: me)? my reminders|(?:arata-mi|afiseaza-mi|lista) (?:reminderele|reminderele mele|reminderelor)", key)
    if manage:
        return ReminderManageAction('list')
    edit_text = re.fullmatch(r'(?:change|edit|modifica|schimba) (?:text|message|textul|mesajul)(?: for| pentru)? reminder(?:ului|ul)? ?#?(\d+) (?:to|cu|in) (.+)', key)
    if edit_text:
        return ReminderManageAction('edit',int(edit_text[1]),text=original(edit_text.start(2)))
    manage = re.fullmatch(r"(cancel|delete|anuleaza|sterge|snooze|amana|edit|modifica) (?:reminder(?:ul)? ?)?#?(\d+)(?: (?:to|la|peste|in) (.+))?", key)
    if manage:
        operation = {'cancel':'cancel','delete':'cancel','anuleaza':'cancel','sterge':'cancel','snooze':'snooze','amana':'snooze','edit':'edit','modifica':'edit'}[manage[1]]
        return ReminderManageAction(operation,int(manage[2]),manage[3])

    # Idiomatic price target phrasing accepts product titles and IDs as references.
    target = re.fullmatch(r"(?:anunta-ma cand|notify me when) (.+?) (?:scade|scad|drops|falls) (?:sub|below) (\S+)(?: (\S+))?", key)
    if target:
        price = number(target[2], lang)
        reference = original(target.start(1),target.end(1))
        if price is None and re.fullmatch(r'\d+[.,]\d{3}',target[2]):
            return NaturalClarification('price','target',{'reference':reference,'currency':CURRENCY_ALIASES.get(target[3]) if target[3] else None},'number')
        if price is None or price <= 0:
            return NaturalCommandError(t('number',lang),'number')
        if not target[3]:
            return NaturalClarification('currency','target',{'reference':reference,'price':price},'currency')
        currency = CURRENCY_ALIASES.get(target[3])
        if currency is None:
            return NaturalCommandError(t('currency',lang),'currency')
        return WishlistTargetAction(reference,price,currency)
    wishlist_short = re.fullmatch(r'lista (?:mea )?de dorinte(?: (?:in) (\S+))?|wishlist', key)
    if wishlist_short:
        currency = CURRENCY_ALIASES.get(wishlist_short[1]) if wishlist_short[1] else None
        if wishlist_short[1] and currency is None:
            return NaturalCommandError(t('currency',lang),'currency')
        return ShowWishlistAction(currency)
    key = re.sub(r'^(arata|afiseaza) mi ', r'\1-mi ', key)
    # Translate aliases for deterministic legacy recognition, not user content.
    key = re.sub(r"lista (?:mea )?de dorinte(?: mea)?", "wishlist-ul", key)
    key = re.sub(r"(?:wishlistul|wishlist ul)", "wishlist-ul", key)
    key = re.sub(r"^(?:grafic pentru|compara|actualizeaza) wishlist-ul(?= |$)",lambda m:m[0]+" meu" if not key[m.end():].startswith(" meu") else m[0],key)
    key = re.sub(r"^ce produse urmaresc\??$", "arata-mi wishlist-ul", key)
    key = re.sub(r"^verifica pretul(?: pentru)? ", "actualizeaza ", key)
    key = re.sub(r"^adauga(?: in wishlist-ul)? ", "urmareste ", key)
    # Recover original URLs before delegating. Romanian folding must not change paths.
    value=value.rstrip(".!? ")
    original_urls = _urls(value)
    folded_urls = _urls(key)
    for normalized, original in zip(folded_urls, original_urls):
        key = key.replace(normalized, original, 1)
    key = re.sub(r"\b(lei|leu|euro|euros|dolari|dollars|pounds|lire)\b", lambda m:CURRENCY_ALIASES[m[1]],key)
    # Preserve localized monetary grouping on legacy target commands.
    price_match = re.search(r"(?: la| to) (\S+) (RON|EUR|DKK|USD|GBP)$", key, re.I)
    if price_match:
        amount = number(price_match[1],lang)
        if amount is None:
            target_parts = re.fullmatch(r'(?:set target price for|seteaza pretul tinta pentru) (.+?) (?:to|la) (\S+) (RON|EUR|DKK|USD|GBP)',key,re.I)
            if target_parts and re.fullmatch(r'\d+[.,]\d{3}',target_parts[2]):
                return NaturalClarification('price','target',{'reference':target_parts[1],'currency':target_parts[3].upper()},'number')
            return NaturalCommandError(t('number',lang),'number')
        key = key[:price_match.start(1)] + str(amount) + key[price_match.end(1):]
    reference_patterns = (
        (r"(?:stop tracking|nu mai urmari|sterge din wishlist-ul) (.+)",DeleteWishlistAction),
        (r"(?:clear target price for|sterge pretul tinta pentru) (.+)",ClearWishlistTargetAction),
    )
    for pattern,kind in reference_patterns:
        match=re.fullmatch(pattern,key)
        if match and not _urls(match[1]):
            return kind(match[1])
    target_reference=re.fullmatch(r"(?:set target price for|seteaza pretul tinta pentru) (.+?) (?:to|la) (\S+) (\S+)",key)
    if target_reference and not _urls(target_reference[1]):
        amount=number(target_reference[2],lang)
        currency=CURRENCY_ALIASES.get(target_reference[3].casefold())
        if not amount or amount<=0:
            return NaturalCommandError(t('number',lang),'number')
        if not currency:
            return NaturalCommandError(t('currency',lang),'currency')
        return WishlistTargetAction(target_reference[1],amount,currency)
    if not _urls(key):
        refresh = re.fullmatch(r'(?:refresh|actualizeaza) (.+)', key)
        if refresh and refresh[1] not in ('my wishlist', 'wishlist-ul meu'):
            return RefreshWishlistAction(refresh[1])
        restock = re.fullmatch(r'(enable|disable) restock only for (.+)|(activeaza|dezactiveaza) doar notificarile de stoc pentru (.+)', key)
        if restock:
            return WishlistRestockAction(restock[2] or restock[4], (restock[1] or restock[3]) in ('enable','activeaza'))
        graph = re.fullmatch(r'(?:graph|grafic pentru) (.+?)(?= in | for | pentru |$)(.*)', key)
        if graph and graph[1] not in ('my wishlist','wishlist-ul meu'):
            currency, days, error = _graph_options(graph[2])
            return error or WishlistGraphAction(graph[1],currency,days)
    parsed = _parse_legacy_command(key, replied_text=replied_text)
    # More tracking verbs and title references still use the same owner-scoped helpers.
    if isinstance(parsed,NaturalCommandError):
        track = re.fullmatch(r"(?:urmareste|monitorizeaza|track|watch|monitor)(?: (.+))?", key)
        if track and not track[1] and not replied_text:
            return NaturalClarification('reference','track',{},'product')
    return parsed
