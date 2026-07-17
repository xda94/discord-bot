"""SerpApi Google Flights client used by the Discord flight tracker.

This module deliberately has no Discord imports. It owns fixed-date validation,
API-key validation, HTTP requests, and normalising Google Flights results into a
small ``FlightOffer`` value that is easy to persist and test.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date

import requests


IATA_RE = re.compile(r"^[A-Z]{3}$")
SUPPORTED_CURRENCIES = ("EUR", "RON", "USD", "GBP", "DKK")


class FlightProviderError(RuntimeError):
    """A provider request could not be completed."""


class FlightProviderConfigError(FlightProviderError):
    """The current Discord user has no SerpApi key configured."""


class FlightProviderQuotaError(FlightProviderError):
    """SerpApi rejected the request because its search quota was reached."""


class NoFlightOffers(FlightProviderError):
    """The request succeeded but returned no usable flight offers."""


@dataclass(frozen=True)
class FlightOffer:
    total_price: float
    currency: str
    departure_date: str
    return_date: str
    airlines: tuple[str, ...] = ()
    stops: int | None = None
    source: str = "serpapi_google_flights"


def normalize_iata(value: str) -> str:
    """Return an upper-case three-letter IATA city/airport code."""
    code = (value or "").strip().upper()
    if not IATA_RE.fullmatch(code):
        raise ValueError("Use a 3-letter IATA city/airport code, for example OTP or BKK.")
    return code


def parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError as exc:
        raise ValueError("Dates must use YYYY-MM-DD, for example 2026-12-30.") from exc


class SerpApiFlightProvider:
    """Requests-based client for SerpApi's Google Flights engine."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        account_url: str | None = None,
        timeout: float | None = None,
        session=None,
    ):
        # The key is supplied from the current Discord user's database row.
        # It intentionally never falls back to a process-wide environment key.
        self.api_key = (api_key or "").strip()
        self.base_url = (
            base_url or os.getenv("SERPAPI_BASE_URL", "https://serpapi.com/search.json")
        ).rstrip("/")
        self.account_url = (
            account_url or os.getenv("SERPAPI_ACCOUNT_URL", "https://serpapi.com/account.json")
        ).rstrip("/")
        self.timeout = float(timeout or os.getenv("SERPAPI_TIMEOUT", "30"))
        self.gl = os.getenv("SERPAPI_GL", "ro").strip().lower() or "ro"
        self.hl = os.getenv("SERPAPI_HL", "en").strip().lower() or "en"
        self.session = session or requests.Session()

    def _require_key(self) -> None:
        if not self.api_key:
            raise FlightProviderConfigError(
                "Your SerpApi API Key is not configured."
            )

    def _get_json(self, url: str, params: dict, context: str) -> dict:
        self._require_key()
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise FlightProviderError(f"Could not reach SerpApi: {exc}") from exc

        payload = None
        try:
            payload = response.json()
        except ValueError:
            pass

        error = payload.get("error") if isinstance(payload, dict) else None
        error_text = str(error or "")
        if response.status_code == 429 or any(
            token in error_text.lower() for token in ("quota", "limit", "searches")
        ):
            raise FlightProviderQuotaError(error_text or "SerpApi search quota reached.")
        if response.status_code >= 400:
            raise FlightProviderError(error_text or f"{context} failed (HTTP {response.status_code}).")
        if not isinstance(payload, dict):
            raise FlightProviderError(f"{context} returned invalid JSON.")
        if error:
            raise FlightProviderError(f"{context} failed: {error}")
        return payload

    def validate_credentials(self) -> bool:
        """Validate the user's key through SerpApi's Account API."""
        self._get_json(
            self.account_url,
            {"api_key": self.api_key},
            "SerpApi key validation",
        )
        return True

    def search_exact(
        self,
        origin: str,
        destination: str,
        departure_date: str,
        return_date: str,
        adults: int = 1,
        currency: str = "EUR",
    ) -> FlightOffer:
        origin = normalize_iata(origin)
        destination = normalize_iata(destination)
        departure = parse_iso_date(departure_date)
        returning = parse_iso_date(return_date)
        if returning <= departure:
            raise ValueError("The return date must be after the departure date.")

        currency_code = currency.upper()
        payload = self._get_json(
            self.base_url,
            {
                "engine": "google_flights",
                "api_key": self.api_key,
                "departure_id": origin,
                "arrival_id": destination,
                "outbound_date": departure.isoformat(),
                "return_date": returning.isoformat(),
                "type": 1,
                "adults": int(adults),
                "currency": currency_code,
                "sort_by": 2,
                "hl": self.hl,
                "gl": self.gl,
            },
            "Google Flights search",
        )

        rows = list(payload.get("best_flights") or [])
        rows.extend(payload.get("other_flights") or [])
        offers = [
            parsed
            for row in rows
            if (parsed := self._parse_offer(
                row, currency_code, departure.isoformat(), returning.isoformat()
            ))
        ]
        if not offers:
            raise NoFlightOffers(
                f"No Google Flights results found for {origin}-{destination} on "
                f"{departure.isoformat()} to {returning.isoformat()}."
            )
        return min(offers, key=lambda offer: offer.total_price)

    @staticmethod
    def _parse_offer(
        row: dict, currency: str, departure_date: str, return_date: str
    ) -> FlightOffer | None:
        try:
            price = float(row["price"])
        except (KeyError, TypeError, ValueError):
            return None

        segments = row.get("flights") or []
        airlines = []
        for segment in segments:
            airline = segment.get("airline")
            if airline and airline not in airlines:
                airlines.append(str(airline))
        stops = max(0, len(segments) - 1) if segments else None
        return FlightOffer(
            total_price=price,
            currency=currency,
            departure_date=departure_date,
            return_date=return_date,
            airlines=tuple(airlines),
            stops=stops,
        )
