"""Amadeus Self-Service client used by the Discord flight tracker.

This module deliberately has no Discord imports.  It owns validation, date-window
generation, OAuth, HTTP requests, and normalising Amadeus responses into one small
``FlightOffer`` value that is easy to persist and test.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta

import requests


IATA_RE = re.compile(r"^[A-Z]{3}$")
SUPPORTED_CURRENCIES = ("EUR", "RON", "USD", "GBP", "DKK")


class FlightProviderError(RuntimeError):
    """A provider request could not be completed."""


class FlightProviderConfigError(FlightProviderError):
    """Required Amadeus credentials are not configured."""


class FlightProviderQuotaError(FlightProviderError):
    """Amadeus rejected the request because a quota/rate limit was reached."""


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
    source: str = "amadeus"


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


def build_date_windows(start_date: str, end_date: str, trip_days: int) -> list[tuple[str, str]]:
    """Build inclusive sliding windows such as Oct 1-10, Oct 2-11, ...

    ``trip_days`` is expressed in calendar days because that matches how a user
    describes a "10 day window".  The underlying Amadeus duration is therefore
    ``trip_days - 1`` (the difference between departure and return dates).
    """
    start = parse_iso_date(start_date)
    end = parse_iso_date(end_date)
    if end <= start:
        raise ValueError("The end date must be after the start date.")
    if not 2 <= int(trip_days) <= 31:
        raise ValueError("trip_days must be between 2 and 31.")

    last_departure = end - timedelta(days=trip_days - 1)
    if last_departure < start:
        raise ValueError("The selected period is shorter than trip_days.")

    windows: list[tuple[str, str]] = []
    departure = start
    while departure <= last_departure:
        windows.append(
            (departure.isoformat(), (departure + timedelta(days=trip_days - 1)).isoformat())
        )
        departure += timedelta(days=1)
    return windows


class AmadeusFlightProvider:
    """Small requests-based client for Amadeus Self-Service flight search."""

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        session=None,
    ):
        self.client_id = client_id if client_id is not None else os.getenv("AMADEUS_CLIENT_ID", "")
        self.client_secret = (
            client_secret if client_secret is not None else os.getenv("AMADEUS_CLIENT_SECRET", "")
        )
        self.base_url = (base_url or os.getenv("AMADEUS_BASE_URL", "https://test.api.amadeus.com")).rstrip("/")
        self.timeout = float(timeout or os.getenv("AMADEUS_TIMEOUT", "20"))
        self.session = session or requests.Session()
        self._access_token: str | None = None
        self._token_expires_at = 0.0

    def _get_access_token(self, force_refresh: bool = False) -> str:
        if not self.client_id or not self.client_secret:
            raise FlightProviderConfigError(
                "Set AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET in .env."
            )
        if not force_refresh and self._access_token and time.time() < self._token_expires_at:
            return self._access_token

        try:
            response = self.session.post(
                f"{self.base_url}/v1/security/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise FlightProviderError(f"Could not reach Amadeus: {exc}") from exc

        if response.status_code >= 400:
            raise FlightProviderError(self._error_message(response, "Amadeus authentication failed"))
        try:
            payload = response.json()
            token = payload["access_token"]
            expires_in = max(1, int(payload.get("expires_in", 1800)))
        except (ValueError, KeyError, TypeError) as exc:
            raise FlightProviderError("Amadeus returned an invalid authentication response.") from exc

        self._access_token = token
        # Refresh one minute early so a token cannot expire during a search.
        self._token_expires_at = time.time() + max(1, expires_in - 60)
        return token

    def _get(self, path: str, params: dict) -> dict:
        token = self._get_access_token()
        for attempt in range(2):
            try:
                response = self.session.get(
                    f"{self.base_url}{path}",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                raise FlightProviderError(f"Could not reach Amadeus: {exc}") from exc

            if response.status_code == 401 and attempt == 0:
                token = self._get_access_token(force_refresh=True)
                continue
            if response.status_code == 429:
                raise FlightProviderQuotaError(
                    self._error_message(response, "Amadeus quota or rate limit reached")
                )
            if response.status_code >= 400:
                raise FlightProviderError(self._error_message(response, "Amadeus search failed"))
            try:
                return response.json()
            except ValueError as exc:
                raise FlightProviderError("Amadeus returned invalid JSON.") from exc
        raise FlightProviderError("Amadeus authentication failed after refreshing the token.")

    @staticmethod
    def _error_message(response, fallback: str) -> str:
        try:
            payload = response.json()
            errors = payload.get("errors") or []
            if errors:
                detail = errors[0].get("detail") or errors[0].get("title")
                if detail:
                    return f"{fallback}: {detail}"
        except (ValueError, AttributeError, TypeError):
            pass
        return f"{fallback} (HTTP {response.status_code})."

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

        payload = self._get(
            "/v2/shopping/flight-offers",
            {
                "originLocationCode": origin,
                "destinationLocationCode": destination,
                "departureDate": departure.isoformat(),
                "returnDate": returning.isoformat(),
                "adults": int(adults),
                "currencyCode": currency.upper(),
                "max": 20,
            },
        )
        offers = [
            parsed
            for item in payload.get("data", [])
            if (parsed := self._parse_live_offer(item, departure.isoformat(), returning.isoformat()))
        ]
        if not offers:
            raise NoFlightOffers(
                f"No flights found for {origin}-{destination} on "
                f"{departure.isoformat()} to {returning.isoformat()}."
            )
        return min(offers, key=lambda offer: offer.total_price)

    def search_flexible(
        self,
        origin: str,
        destination: str,
        start_date: str,
        end_date: str,
        trip_days: int,
        adults: int = 1,
        currency: str = "EUR",
        confirmation_limit: int = 3,
    ) -> FlightOffer:
        """Find the cheapest date pair in a period, then confirm it live.

        Amadeus' cheapest-date API searches the entire departure range in one
        cached request.  We then confirm the best candidates with the live
        Flight Offers Search endpoint; this keeps flexible trackers useful
        without issuing one request for every day on every five-hour pass.
        """
        origin = normalize_iata(origin)
        destination = normalize_iata(destination)
        windows = build_date_windows(start_date, end_date, trip_days)
        first_departure = windows[0][0]
        last_departure = windows[-1][0]
        payload = self._get(
            "/v1/shopping/flight-dates",
            {
                "origin": origin,
                "destination": destination,
                "departureDate": f"{first_departure},{last_departure}",
                "duration": int(trip_days) - 1,
                "oneWay": "false",
                "currency": currency.upper(),
                "viewBy": "DURATION",
            },
        )

        candidates = []
        for item in payload.get("data", []):
            try:
                price = float(item["price"]["total"])
                departure = parse_iso_date(item["departureDate"])
                returning = parse_iso_date(item["returnDate"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (parse_iso_date(first_departure) <= departure <= parse_iso_date(last_departure)):
                continue
            if (returning - departure).days != int(trip_days) - 1:
                continue
            candidates.append((price, departure.isoformat(), returning.isoformat()))

        if not candidates:
            raise NoFlightOffers(
                "Amadeus has no cached flexible-date results for this route and period."
            )

        last_error: NoFlightOffers | None = None
        for _price, departure, returning in sorted(candidates)[: max(1, confirmation_limit)]:
            try:
                return self.search_exact(
                    origin, destination, departure, returning, adults, currency
                )
            except NoFlightOffers as exc:
                last_error = exc
        raise last_error or NoFlightOffers("No live flight offers matched the flexible period.")

    @staticmethod
    def _parse_live_offer(item: dict, departure_date: str, return_date: str) -> FlightOffer | None:
        try:
            total = float(item["price"]["grandTotal"])
            currency = str(item["price"]["currency"]).upper()
        except (KeyError, TypeError, ValueError):
            return None

        airlines = tuple(dict.fromkeys(item.get("validatingAirlineCodes") or ()))
        itineraries = item.get("itineraries") or []
        if not airlines:
            carriers = []
            for itinerary in itineraries:
                for segment in itinerary.get("segments") or []:
                    carrier = segment.get("carrierCode")
                    if carrier and carrier not in carriers:
                        carriers.append(carrier)
            airlines = tuple(carriers)
        stops = None
        if itineraries:
            stops = sum(max(0, len(itinerary.get("segments") or []) - 1) for itinerary in itineraries)
        return FlightOffer(
            total_price=total,
            currency=currency,
            departure_date=departure_date,
            return_date=return_date,
            airlines=airlines,
            stops=stops,
        )
