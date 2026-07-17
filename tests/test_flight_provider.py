from datetime import date, timedelta

import pytest

from flight_provider import (
    AmadeusFlightProvider,
    FlightProviderConfigError,
    build_date_windows,
    normalize_iata,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, get_responses=None):
        self.get_responses = list(get_responses or [])
        self.posts = []
        self.gets = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return FakeResponse({"access_token": "token", "expires_in": 1800})

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return self.get_responses.pop(0)


def _live_payload(price="500.00", currency="EUR"):
    return {
        "data": [
            {
                "price": {"grandTotal": price, "currency": currency},
                "validatingAirlineCodes": ["TK"],
                "itineraries": [
                    {"segments": [{"carrierCode": "TK"}, {"carrierCode": "TK"}]},
                    {"segments": [{"carrierCode": "TK"}]},
                ],
            }
        ]
    }


def test_build_date_windows_matches_requested_sliding_range():
    windows = build_date_windows("2030-10-01", "2030-10-31", 10)
    assert len(windows) == 22
    assert windows[0] == ("2030-10-01", "2030-10-10")
    assert windows[1] == ("2030-10-02", "2030-10-11")
    assert windows[-1] == ("2030-10-22", "2030-10-31")


def test_build_date_windows_rejects_period_shorter_than_trip():
    with pytest.raises(ValueError, match="shorter"):
        build_date_windows("2030-10-01", "2030-10-05", 10)


def test_normalize_iata():
    assert normalize_iata(" otp ") == "OTP"
    with pytest.raises(ValueError, match="3-letter"):
        normalize_iata("Bucharest")


def test_missing_credentials_fail_with_actionable_message():
    provider = AmadeusFlightProvider(client_id="", client_secret="", session=FakeSession())
    with pytest.raises(FlightProviderConfigError, match="AMADEUS_CLIENT_ID"):
        provider.search_exact("OTP", "BKK", "2030-12-30", "2031-01-13")


def test_search_exact_returns_cheapest_normalized_offer():
    session = FakeSession([FakeResponse(_live_payload("650.25", "EUR"))])
    provider = AmadeusFlightProvider(
        client_id="id", client_secret="secret", session=session, base_url="https://example.test"
    )
    offer = provider.search_exact("OTP", "BKK", "2030-12-30", "2031-01-13")

    assert offer.total_price == 650.25
    assert offer.currency == "EUR"
    assert offer.departure_date == "2030-12-30"
    assert offer.return_date == "2031-01-13"
    assert offer.airlines == ("TK",)
    assert offer.stops == 1
    assert session.gets[0][1]["params"]["originLocationCode"] == "OTP"
    assert len(session.posts) == 1


def test_search_flexible_queries_range_and_confirms_best_candidate_live():
    cached = {
        "data": [
            {
                "departureDate": "2030-10-07",
                "returnDate": "2030-10-16",
                "price": {"total": "480.00"},
            },
            {
                "departureDate": "2030-10-03",
                "returnDate": "2030-10-12",
                "price": {"total": "520.00"},
            },
        ]
    }
    session = FakeSession(
        [FakeResponse(cached), FakeResponse(_live_payload("495.00", "EUR"))]
    )
    provider = AmadeusFlightProvider(
        client_id="id", client_secret="secret", session=session, base_url="https://example.test"
    )

    offer = provider.search_flexible(
        "OTP", "BKK", "2030-10-01", "2030-10-31", 10, 1, "EUR"
    )

    flexible_params = session.gets[0][1]["params"]
    assert flexible_params["departureDate"] == "2030-10-01,2030-10-22"
    assert flexible_params["duration"] == 9
    assert offer.departure_date == "2030-10-07"
    assert offer.return_date == "2030-10-16"
    assert offer.total_price == 495.0

