import pytest

from flight_provider import (
    FlightProviderConfigError,
    FlightProviderQuotaError,
    SerpApiFlightProvider,
    normalize_iata,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.gets = []

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return self.responses.pop(0)


def _flights_payload():
    return {
        "search_metadata": {"status": "Success"},
        "best_flights": [
            {
                "price": 650,
                "flights": [
                    {"airline": "Turkish Airlines"},
                    {"airline": "Turkish Airlines"},
                ],
            }
        ],
        "other_flights": [
            {
                "price": 599,
                "flights": [{"airline": "KLM"}],
            }
        ],
    }


def test_normalize_iata():
    assert normalize_iata(" otp ") == "OTP"
    with pytest.raises(ValueError, match="3-letter"):
        normalize_iata("Bucharest")


def test_missing_key_fails_with_actionable_message():
    provider = SerpApiFlightProvider(api_key="", session=FakeSession())
    with pytest.raises(FlightProviderConfigError, match="SerpApi API Key"):
        provider.search_exact("OTP", "BKK", "2030-12-30", "2031-01-13")


def test_validate_credentials_uses_account_api():
    session = FakeSession([FakeResponse({"account_id": "abc", "plan_name": "Free"})])
    provider = SerpApiFlightProvider(
        api_key="personal-key",
        session=session,
        account_url="https://example.test/account.json",
    )

    assert provider.validate_credentials() is True
    assert session.gets[0][0] == "https://example.test/account.json"
    assert session.gets[0][1]["params"] == {"api_key": "personal-key"}


def test_search_exact_returns_cheapest_google_flights_offer(monkeypatch):
    monkeypatch.setenv("SERPAPI_GL", "ro")
    session = FakeSession([FakeResponse(_flights_payload())])
    provider = SerpApiFlightProvider(
        api_key="personal-key",
        session=session,
        base_url="https://example.test/search.json",
    )

    offer = provider.search_exact("OTP", "BKK", "2030-12-30", "2031-01-13", 2, "EUR")

    assert offer.total_price == 599
    assert offer.currency == "EUR"
    assert offer.departure_date == "2030-12-30"
    assert offer.return_date == "2031-01-13"
    assert offer.airlines == ("KLM",)
    assert offer.stops == 0
    params = session.gets[0][1]["params"]
    assert params["engine"] == "google_flights"
    assert params["api_key"] == "personal-key"
    assert params["departure_id"] == "OTP"
    assert params["arrival_id"] == "BKK"
    assert params["outbound_date"] == "2030-12-30"
    assert params["return_date"] == "2031-01-13"
    assert params["adults"] == 2
    assert params["sort_by"] == 2


def test_quota_error_is_classified():
    session = FakeSession(
        [FakeResponse({"error": "Your account has run out of searches."}, status_code=429)]
    )
    provider = SerpApiFlightProvider(api_key="key", session=session)
    with pytest.raises(FlightProviderQuotaError, match="searches"):
        provider.search_exact("OTP", "BKK", "2030-12-30", "2031-01-13")
