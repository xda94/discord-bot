"""Anchored, deterministic parsing for the bot's bounded natural commands."""

from __future__ import annotations

import re
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
class NaturalCommandError:
    message: str


NaturalAction = Union[TrackURLAction, ShowFlightsAction, ReminderAction]


def _urls(value: str) -> list[str]:
    return [match.group(0).rstrip(".,;:!?)]}") for match in _URL_RE.finditer(value or "")]


def _number(value: str) -> float | None:
    normalized = value.casefold()
    if normalized in _NUMBER_WORDS:
        return float(_NUMBER_WORDS[normalized])
    try:
        return float(normalized.replace(",", "."))
    except ValueError:
        return None


def parse_natural_command(
    text: str, *, replied_text: str = ""
) -> NaturalAction | NaturalCommandError | None:
    """Parse only explicitly supported full-message phrases.

    Unsupported prose returns ``None`` so the normal mention LLM can handle it.
    Recognized but incomplete/ambiguous commands return actionable guidance.
    """
    value = " ".join((text or "").strip().split())
    if not value:
        return None

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
