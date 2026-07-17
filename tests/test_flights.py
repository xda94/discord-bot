import asyncio
import time
from datetime import date, timedelta

import discord
from discord import app_commands

import db
from features.flights import (
    FlightTrackerFeature,
    _select_trackers_for_pass,
    validate_tracker_input,
)
from flight_provider import FlightOffer, FlightProviderError


class StubProvider:
    def __init__(self, price=599.0):
        self.price = price
        self.exact_calls = []

    def validate_credentials(self):
        return True

    def search_exact(self, *args):
        self.exact_calls.append(args)
        return FlightOffer(
            self.price, "EUR", args[2], args[3], ("TK",), 1,
        )

class RejectingProvider(StubProvider):
    def validate_credentials(self):
        raise FlightProviderError("invalid client")


class FakeResponse:
    def __init__(self):
        self.messages = []
        self.deferred = False
        self.modal = None

    async def defer(self, **kwargs):
        self.deferred = True

    async def send_message(self, content, **kwargs):
        self.messages.append((content, kwargs))

    async def send_modal(self, modal):
        self.modal = modal


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content, **kwargs):
        self.messages.append((content, kwargs))


class FakeInteraction:
    def __init__(self, user_id=123):
        self.user = type("User", (), {"id": user_id})()
        self.response = FakeResponse()
        self.followup = FakeFollowup()


def _future_dates():
    departure = date.today() + timedelta(days=60)
    returning = departure + timedelta(days=14)
    return departure.isoformat(), returning.isoformat()


def _make_feature(provider=None):
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    stub = provider or StubProvider()
    feature = FlightTrackerFeature(
        client, tree, provider_factory=lambda _api_key: stub
    )
    return client, feature, tree


def test_validate_fixed_tracker_normalizes_values():
    start = date.today() + timedelta(days=60)
    end = start + timedelta(days=30)
    values = validate_tracker_input(
        "otp", "bkk", start.isoformat(), end.isoformat(), 2, "eur"
    )
    assert values["origin"] == "OTP"
    assert values["destination"] == "BKK"
    assert values["trip_days"] == 0
    assert values["adults"] == 2


def test_db_flight_tracker_is_per_user_and_delete_cascades_history(tmp_db):
    start, end = _future_dates()
    tracker_id = db.add_flight_tracker(123, "OTP", "BKK", start, end)
    assert tracker_id
    # Same watch is valid for another user, but not duplicated for this user.
    assert db.add_flight_tracker(456, "OTP", "BKK", start, end)
    assert db.add_flight_tracker(123, "OTP", "BKK", start, end) is None

    db.add_flight_price_history(tracker_id, 600, "EUR", start, end)
    db.update_flight_tracker_result(tracker_id, 600, "EUR", start, end, error=None)
    own = db.get_user_flight_trackers(123)
    assert len(own) == 1
    assert own[0]["last_price"] == 600
    assert len(db.get_flight_price_history(tracker_id, 123)) == 1

    assert db.delete_flight_tracker(456, tracker_id) is False
    assert db.delete_flight_tracker(123, tracker_id) is True
    assert db.get_flight_price_history(tracker_id) == []
    assert len(db.get_user_flight_trackers(456)) == 1


def test_serpapi_keys_are_stored_per_user(tmp_db):
    assert db.get_flight_api_credentials(123) is None
    assert db.set_flight_api_credentials(123, "key-a")
    assert db.set_flight_api_credentials(456, "key-b")

    first = db.get_flight_api_credentials(123)
    second = db.get_flight_api_credentials(456)
    assert first["api_key"] == "key-a"
    assert second["api_key"] == "key-b"

    assert db.delete_flight_api_credentials(123) is True
    assert db.get_flight_api_credentials(123) is None
    assert db.get_flight_api_credentials(456)["api_key"] == "key-b"


def test_old_amadeus_credentials_are_removed_by_migration(tmp_db):
    with db._connect(commit=True) as c:
        c.execute("DROP TABLE flight_api_credentials")
        c.execute(
            "CREATE TABLE flight_api_credentials ("
            "user_id INTEGER PRIMARY KEY, client_id TEXT NOT NULL, "
            "client_secret TEXT NOT NULL, updated_at REAL NOT NULL)"
        )
        c.execute(
            "INSERT INTO flight_api_credentials VALUES (?, ?, ?, ?)",
            (123, "old-amadeus-key", "old-amadeus-secret", 1.0),
        )

    db.init_db()
    with db._connect() as c:
        c.execute("PRAGMA table_info(flight_api_credentials)")
        columns = {row[1] for row in c.fetchall()}
    assert columns == {"user_id", "api_key", "updated_at"}
    assert db.get_flight_api_credentials(123) is None


def test_scheduled_pass_selects_only_oldest_tracker_per_user(tmp_db):
    start, end = _future_dates()
    first = db.add_flight_tracker(123, "OTP", "BKK", start, end)
    second = db.add_flight_tracker(123, "OTP", "JFK", start, end)
    third = db.add_flight_tracker(456, "LHR", "BKK", start, end)
    db.update_flight_tracker_result(first, checked_at=100.0, error=None)
    db.update_flight_tracker_result(second, checked_at=200.0, error=None)

    selected = _select_trackers_for_pass(db.get_all_flight_trackers())
    assert {tracker["id"] for tracker in selected} == {first, third}


def test_recent_check_is_not_repeated_after_bot_restart(tmp_db):
    provider = StubProvider()
    client, feature, _tree = _make_feature(provider)
    start, end = _future_dates()
    tracker_id = db.add_flight_tracker(123, "OTP", "BKK", start, end)
    db.set_flight_api_credentials(123, "key-a")
    db.update_flight_tracker_result(tracker_id, checked_at=time.time(), error=None)

    asyncio.run(feature._process_tracker(db.get_flight_tracker(tracker_id)))
    assert provider.exact_calls == []
    asyncio.run(client.close())


def test_add_without_credentials_logs_in_then_adds_tracker(tmp_db):
    provider = StubProvider(price=550.0)
    client, _feature, tree = _make_feature(provider)
    start, end = _future_dates()
    interaction = FakeInteraction(123)

    asyncio.run(
        tree.get_command("flight_tracker_add").callback(
            interaction, "otp", "bkk", start, end, 1, None
        )
    )

    assert interaction.response.modal is not None
    assert interaction.response.modal.title == "Flight Tracker Login"
    assert db.get_user_flight_trackers(123) == []

    modal = interaction.response.modal
    modal.api_key._value = "personal-key"
    submit_interaction = FakeInteraction(123)
    asyncio.run(modal.on_submit(submit_interaction))

    saved = db.get_flight_api_credentials(123)
    assert saved["api_key"] == "personal-key"
    assert db.get_user_flight_trackers(123)[0]["last_price"] == 550.0
    assert "Added flight tracker **#1**" in submit_interaction.followup.messages[0][0]
    asyncio.run(client.close())


def test_invalid_modal_credentials_are_not_saved_and_tracker_is_not_added(tmp_db):
    client, _feature, tree = _make_feature(RejectingProvider())
    start, end = _future_dates()
    interaction = FakeInteraction(123)
    asyncio.run(
        tree.get_command("flight_tracker_add").callback(
            interaction, "otp", "bkk", start, end, 1, None
        )
    )

    modal = interaction.response.modal
    modal.api_key._value = "bad-key"
    submit_interaction = FakeInteraction(123)
    asyncio.run(modal.on_submit(submit_interaction))

    assert db.get_flight_api_credentials(123) is None
    assert db.get_user_flight_trackers(123) == []
    assert "Nothing was saved" in submit_interaction.followup.messages[0][0]
    asyncio.run(client.close())


def test_provider_is_selected_from_each_trackers_user_credentials(tmp_db):
    created = []

    def factory(api_key):
        created.append(api_key)
        return StubProvider()

    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    feature = FlightTrackerFeature(client, tree, provider_factory=factory)
    db.set_flight_api_credentials(123, "key-a")
    db.set_flight_api_credentials(456, "key-b")

    assert feature._provider_for_user(123) is feature._provider_for_user(123)
    feature._provider_for_user(456)
    assert created == ["key-a", "key-b"]
    asyncio.run(client.close())


def test_flight_tracker_add_show_delete_commands_work(tmp_db):
    provider = StubProvider(price=599.0)
    client, _feature, tree = _make_feature(provider)
    start, end = _future_dates()
    db.set_flight_api_credentials(123, "user-key")

    add_interaction = FakeInteraction(123)
    asyncio.run(
        tree.get_command("flight_tracker_add").callback(
            add_interaction, "otp", "bkk", start, end, 1, None
        )
    )
    assert add_interaction.response.deferred is True
    assert "Added flight tracker **#1**" in add_interaction.followup.messages[0][0]
    assert provider.exact_calls
    assert db.get_user_flight_trackers(123)[0]["last_price"] == 599.0

    show_interaction = FakeInteraction(123)
    asyncio.run(tree.get_command("flight_tracker_show").callback(show_interaction))
    assert "#1 OTP -> BKK" in show_interaction.response.messages[0][0]
    assert "599.00 EUR" in show_interaction.response.messages[0][0]

    # Ownership is enforced by both command and SQL predicate.
    wrong_user = FakeInteraction(999)
    asyncio.run(tree.get_command("flight_tracker_delete").callback(wrong_user, 1))
    assert "not found" in wrong_user.response.messages[0][0].lower()

    delete_interaction = FakeInteraction(123)
    asyncio.run(tree.get_command("flight_tracker_delete").callback(delete_interaction, 1))
    assert "were deleted" in delete_interaction.response.messages[0][0]
    assert db.get_user_flight_trackers(123) == []
    asyncio.run(client.close())
