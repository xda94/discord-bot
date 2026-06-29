"""Tests for the /azi-se-spala feature (`features.azi_se_spala`).

Three layers, none of which touch the network:
  * pure helpers — `_parse_date`, `_lookup_day`, `_format_reply`
  * `_fetch_calendar` with `requests.get` monkeypatched (caching + the
    error mapping that turns 404 / non-JSON into `CalendarNotAvailable`)
  * the async `_run` handler, exercised end-to-end against a mock
    interaction (via `asyncio.run`, so no pytest-asyncio needed) with
    `_fetch_calendar` patched out.
"""

import asyncio
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
import requests

from features import azi_se_spala as m

# A trimmed stand-in for the real calendar JSON: one no-wash holy day, one
# ordinary wash day, both in June so the month key exists but day 28 doesn't.
SAMPLE_CALENDAR = {
    "6": [
        {"date": "29", "text": "Sf. Ap. Petru și Pavel", "isHoliday": True, "noWashing": True},
        {"date": "30", "text": "Soborul Sf. Apostoli", "isHoliday": False, "noWashing": False},
    ],
}


class _FakeResponse:
    """Minimal stand-in for a `requests.Response`."""

    def __init__(self, *, status_code=200, json_data=None, json_error=False):
        self.status_code = status_code
        self._json_data = json_data
        self._json_error = json_error

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def json(self):
        if self._json_error:
            raise ValueError("not valid json")
        return self._json_data


def _make_interaction():
    """A mock discord.Interaction whose async methods are awaitable."""
    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


@pytest.fixture(autouse=True)
def _clear_cache():
    """The per-year calendar cache is a process-wide global; clear it around
    every test so cached data can't leak between cases."""
    m._calendar_cache.clear()
    yield
    m._calendar_cache.clear()


# ---------------------------------------------------------------------------
# _parse_date
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [None, "", "   "])
def test_parse_date_blank_is_today(raw):
    assert m._parse_date(raw) == datetime.now().date()


@pytest.mark.parametrize("raw", ["25.12", "25-12", "25/12"])
def test_parse_date_day_only_pins_current_year(raw):
    assert m._parse_date(raw) == date(datetime.now().year, 12, 25)


def test_parse_date_strips_whitespace():
    assert m._parse_date("  25.12  ") == date(datetime.now().year, 12, 25)


@pytest.mark.parametrize("raw", ["25.12.2030", "25-12-2030", "25/12/2030"])
def test_parse_date_explicit_year_is_honoured(raw):
    assert m._parse_date(raw) == date(2030, 12, 25)


@pytest.mark.parametrize("raw", ["garbage", "32.13", "00.00", "2030-12-25", "abc.de"])
def test_parse_date_invalid_returns_none(raw):
    assert m._parse_date(raw) is None


# ---------------------------------------------------------------------------
# _lookup_day
# ---------------------------------------------------------------------------

def test_lookup_day_found():
    entry = m._lookup_day(SAMPLE_CALENDAR, date(2026, 6, 29))
    assert entry is not None and entry["text"] == "Sf. Ap. Petru și Pavel"


def test_lookup_day_missing_day_in_present_month():
    assert m._lookup_day(SAMPLE_CALENDAR, date(2026, 6, 28)) is None


def test_lookup_day_missing_month():
    assert m._lookup_day(SAMPLE_CALENDAR, date(2026, 1, 1)) is None


# ---------------------------------------------------------------------------
# _format_reply
# ---------------------------------------------------------------------------

def test_format_reply_no_washing():
    out = m._format_reply(date(2026, 6, 29), SAMPLE_CALENDAR["6"][0])
    assert "🚫" in out and "NU se spală" in out
    assert "Sf. Ap. Petru și Pavel" in out
    assert m.ATTRIBUTION in out


def test_format_reply_washing():
    out = m._format_reply(date(2026, 6, 30), SAMPLE_CALENDAR["6"][1])
    assert "✅" in out and "se spală" in out
    assert "🚫" not in out
    assert m.ATTRIBUTION in out


def test_format_reply_keys_off_no_washing_not_is_holiday():
    """A day flagged isHoliday but not noWashing is still a wash day — the
    verdict tracks `noWashing` alone."""
    entry = {"date": "1", "text": "Sărbătoare fără post", "isHoliday": True, "noWashing": False}
    assert "se spală" in m._format_reply(date(2026, 1, 1), entry)


def test_format_reply_missing_no_washing_key_defaults_to_washing():
    entry = {"date": "1", "text": "ceva"}
    out = m._format_reply(date(2026, 1, 1), entry)
    assert "✅" in out


def test_format_reply_without_text_omits_book_line():
    entry = {"date": "1", "text": "", "noWashing": True}
    out = m._format_reply(date(2026, 1, 1), entry)
    assert "📖" not in out
    assert m.ATTRIBUTION in out


def test_attribution_exact_string():
    assert m.ATTRIBUTION == "Date calendar: azisespala.ro"


# ---------------------------------------------------------------------------
# _fetch_calendar (requests mocked)
# ---------------------------------------------------------------------------

def test_fetch_calendar_success_and_caches(monkeypatch):
    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        return _FakeResponse(json_data=SAMPLE_CALENDAR)

    monkeypatch.setattr(m.requests, "get", fake_get)

    first = m._fetch_calendar(2026)
    second = m._fetch_calendar(2026)

    assert first == SAMPLE_CALENDAR
    assert second is first          # served from cache
    assert len(calls) == 1          # only one network round-trip


def test_fetch_calendar_404_raises_not_available(monkeypatch):
    monkeypatch.setattr(m.requests, "get", lambda url, timeout=None: _FakeResponse(status_code=404))
    with pytest.raises(m.CalendarNotAvailable):
        m._fetch_calendar(2099)


def test_fetch_calendar_non_json_raises_not_available(monkeypatch):
    """Unpublished years come back as a 200 HTML page, not a 404."""
    monkeypatch.setattr(m.requests, "get", lambda url, timeout=None: _FakeResponse(json_error=True))
    with pytest.raises(m.CalendarNotAvailable):
        m._fetch_calendar(2099)


def test_fetch_calendar_server_error_propagates(monkeypatch):
    monkeypatch.setattr(m.requests, "get", lambda url, timeout=None: _FakeResponse(status_code=500))
    with pytest.raises(requests.RequestException):
        m._fetch_calendar(2026)


def test_fetch_calendar_network_error_propagates(monkeypatch):
    def boom(url, timeout=None):
        raise requests.ConnectionError("network down")

    monkeypatch.setattr(m.requests, "get", boom)
    with pytest.raises(requests.RequestException):
        m._fetch_calendar(2026)


# ---------------------------------------------------------------------------
# _run (async handler; _fetch_calendar patched, no network)
# ---------------------------------------------------------------------------

def test_run_no_washing_day(monkeypatch):
    monkeypatch.setattr(m, "_fetch_calendar", lambda year: SAMPLE_CALENDAR)
    interaction = _make_interaction()

    asyncio.run(m._run(interaction, "29.06.2026"))

    interaction.response.defer.assert_awaited_once()
    interaction.response.send_message.assert_not_called()
    interaction.followup.send.assert_awaited_once()
    sent = interaction.followup.send.call_args.args[0]
    assert "NU se spală" in sent
    assert m.ATTRIBUTION in sent


def test_run_washing_day(monkeypatch):
    monkeypatch.setattr(m, "_fetch_calendar", lambda year: SAMPLE_CALENDAR)
    interaction = _make_interaction()

    asyncio.run(m._run(interaction, "30.06.2026"))

    sent = interaction.followup.send.call_args.args[0]
    assert "se spală" in sent and "NU se spală" not in sent


def test_run_invalid_date_replies_ephemeral_without_fetching(monkeypatch):
    def fail(_year):
        raise AssertionError("should not fetch on an invalid date")

    monkeypatch.setattr(m, "_fetch_calendar", fail)
    interaction = _make_interaction()

    asyncio.run(m._run(interaction, "garbage"))

    interaction.response.send_message.assert_awaited_once()
    assert interaction.response.send_message.call_args.kwargs.get("ephemeral") is True
    interaction.response.defer.assert_not_awaited()
    interaction.followup.send.assert_not_called()


def test_run_calendar_not_available(monkeypatch):
    def raise_unavailable(year):
        raise m.CalendarNotAvailable(year)

    monkeypatch.setattr(m, "_fetch_calendar", raise_unavailable)
    interaction = _make_interaction()

    asyncio.run(m._run(interaction, "01.01.2099"))

    interaction.response.defer.assert_awaited_once()
    assert "Nu am date din calendar" in interaction.followup.send.call_args.args[0]


def test_run_network_error(monkeypatch):
    def raise_network(_year):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(m, "_fetch_calendar", raise_network)
    interaction = _make_interaction()

    asyncio.run(m._run(interaction, "29.06.2026"))

    assert "Nu am putut contacta" in interaction.followup.send.call_args.args[0]


def test_run_day_not_in_calendar(monkeypatch):
    monkeypatch.setattr(m, "_fetch_calendar", lambda year: SAMPLE_CALENDAR)
    interaction = _make_interaction()

    asyncio.run(m._run(interaction, "28.06.2026"))

    assert "Nu am găsit nicio intrare" in interaction.followup.send.call_args.args[0]
