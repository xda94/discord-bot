import asyncio
import logging
from datetime import date, datetime

import discord
import requests
from discord import app_commands

logger = logging.getLogger("discord_bot")

# azisespala.ro publishes the Romanian Orthodox calendar one JSON file per
# year. Each file is keyed by month ("1".."12"); every month is an array of
# day entries {date, text, isHoliday, noWashing}. `noWashing` is the bit this
# command exists for: by tradition you don't do laundry on certain holy days.
CALENDAR_URL = "https://azisespala.ro/data/holidays-{year}.json"

# Accept the day in the formats a Romanian would actually type. All resolve to
# (day, month); the year always comes from the requested/`today` year so we
# only ever fetch one calendar file.
_DATE_FORMATS = ("%d.%m", "%d-%m", "%d/%m", "%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y")

# Per-year in-memory cache. The calendar is published once a year and never
# changes mid-year, so there's no reason to re-fetch within a process lifetime.
# Keyed by int year -> parsed JSON dict.
_calendar_cache: dict[int, dict] = {}


class CalendarNotAvailable(Exception):
    """The calendar for the requested year isn't published yet. The site
    doesn't 404 on unknown years — it serves a non-JSON page — so this also
    covers the "response wasn't valid JSON" case. Distinct from a network
    failure so the command can say "no data for that year" vs "try later"."""


def _fetch_calendar(year: int) -> dict:
    """Fetch (and cache) the calendar JSON for `year`.

    Blocking — call via `asyncio.to_thread`. Raises `CalendarNotAvailable`
    for an unpublished year (404 or non-JSON page) and `requests.RequestException`
    on network failure; the caller turns each into its own user-facing message."""
    cached = _calendar_cache.get(year)
    if cached is not None:
        return cached

    response = requests.get(CALENDAR_URL.format(year=year), timeout=10)
    if response.status_code == 404:
        raise CalendarNotAvailable(year)
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:
        # Unpublished years come back as a 200 HTML page, not a 404, so a
        # JSON-decode failure here means "that year isn't out yet".
        raise CalendarNotAvailable(year) from exc
    _calendar_cache[year] = data
    return data


def _lookup_day(calendar: dict, target: date) -> dict | None:
    """Return the calendar entry for `target`, or None if the file has no
    row for that day (shouldn't happen for a complete calendar, but the data
    is third-party so we don't assume)."""
    month_entries = calendar.get(str(target.month))
    if not month_entries:
        return None
    target_day = str(target.day)
    for entry in month_entries:
        if str(entry.get("date")) == target_day:
            return entry
    return None


def _format_reply(target: date, entry: dict) -> str:
    """Build the Romanian reply for a resolved calendar entry."""
    pretty_date = target.strftime("%d.%m.%Y")
    text = (entry.get("text") or "").strip()
    no_washing = bool(entry.get("noWashing"))

    if no_washing:
        header = f"🚫 **{pretty_date} — azi NU se spală!**"
        body = "E zi de sărbătoare, după tradiție nu se spală rufe azi."
    else:
        header = f"✅ **{pretty_date} — azi se spală!**"
        body = "Nicio opreliște azi — dă drumul la mașina de spălat. 🧺"

    lines = [header, body]
    if text:
        lines.append(f"\n📖 {text}")
    return "\n".join(lines)


class AziSeSpalaFeature:
    """The /azi-se-spala command — tells you whether today (or a given date)
    is a day you can do laundry, per the Romanian Orthodox calendar served by
    azisespala.ro."""

    def __init__(self, client: discord.Client, tree: app_commands.CommandTree):
        self.client = client
        self.tree = tree
        self._register_commands()

    def _parse_date(self, raw: str | None) -> date | None:
        """Resolve the optional `data` argument to a `date`.

        None/empty -> today. A recognised day string -> that day this year
        (or the explicit year if the user typed one). Unrecognised -> None,
        which the command surfaces as a friendly format hint."""
        if not raw or not raw.strip():
            return datetime.now().date()
        raw = raw.strip()
        for fmt in _DATE_FORMATS:
            try:
                parsed = datetime.strptime(raw, fmt)
            except ValueError:
                continue
            # Day-only formats default the year to 1900; pin those to the
            # current year so "25.12" means this Christmas, not 1900's.
            if "%Y" not in fmt:
                parsed = parsed.replace(year=datetime.now().year)
            return parsed.date()
        return None

    def _register_commands(self) -> None:
        feature = self

        @self.tree.command(
            name="azi-se-spala",
            description="Vezi dacă azi (sau într-o anumită zi) se spală rufe, după calendarul ortodox",
        )
        @app_commands.describe(
            data="Optional: o zi de verificat (ex. 25.12 sau 25.12.2026). Implicit, azi."
        )
        async def azi_se_spala(interaction: discord.Interaction, data: str | None = None):
            logger.info(f"Command /azi-se-spala called by {interaction.user} (data={data!r})")

            target = feature._parse_date(data)
            if target is None:
                await interaction.response.send_message(
                    "Format de dată nevalid. Folosește `ZZ.LL` sau `ZZ.LL.AAAA` "
                    "(ex. `25.12` sau `25.12.2026`).",
                    ephemeral=True,
                )
                return

            # Network round-trip; defer so we never race Discord's 3s window.
            await interaction.response.defer()

            try:
                calendar = await asyncio.to_thread(_fetch_calendar, target.year)
            except CalendarNotAvailable:
                await interaction.followup.send(
                    f"Nu am date din calendar pentru anul {target.year} "
                    f"(încă nu sunt publicate pe azisespala.ro)."
                )
                return
            except requests.RequestException as exc:
                logger.warning(f"Failed to fetch azisespala calendar: {exc}")
                await interaction.followup.send(
                    "Nu am putut contacta azisespala.ro acum. Încearcă mai târziu."
                )
                return

            entry = _lookup_day(calendar, target)
            if entry is None:
                await interaction.followup.send(
                    f"Nu am găsit nicio intrare pentru {target.strftime('%d.%m.%Y')} "
                    f"în calendar."
                )
                return

            await interaction.followup.send(_format_reply(target, entry))
