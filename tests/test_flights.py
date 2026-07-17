import asyncio
from datetime import date, timedelta

import discord
from discord import app_commands

import db
from features.flights import FlightTrackerFeature, validate_tracker_input
from flight_provider import FlightOffer


class StubProvider:
    def __init__(self, price=599.0):
        self.price = price
        self.exact_calls = []
        self.flexible_calls = []

    def search_exact(self, *args):
        self.exact_calls.append(args)
        return FlightOffer(
            self.price, "EUR", args[2], args[3], ("TK",), 1,
        )

    def search_flexible(self, *args):
        self.flexible_calls.append(args)
        return FlightOffer(
            self.price, "EUR", args[2],
            (date.fromisoformat(args[2]) + timedelta(days=args[4] - 1)).isoformat(),
            ("TK",), 1,
        )


class FakeResponse:
    def __init__(self):
        self.messages = []
        self.deferred = False

    async def defer(self, **kwargs):
        self.deferred = True

    async def send_message(self, content, **kwargs):
        self.messages.append((content, kwargs))


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
    feature = FlightTrackerFeature(client, tree, provider=provider or StubProvider())
    group = tree.get_command("flight-tracker")
    return client, feature, group


def test_validate_flexible_tracker_counts_all_windows():
    start = date.today() + timedelta(days=60)
    end = start + timedelta(days=30)
    values = validate_tracker_input(
        "otp", "bkk", start.isoformat(), end.isoformat(), 10, 2, "eur"
    )
    assert values["origin"] == "OTP"
    assert values["destination"] == "BKK"
    assert values["trip_days"] == 10
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


def test_flight_tracker_add_show_delete_commands_work(tmp_db):
    provider = StubProvider(price=599.0)
    client, _feature, group = _make_feature(provider)
    start, end = _future_dates()

    add_interaction = FakeInteraction(123)
    asyncio.run(
        group.get_command("add").callback(
            add_interaction, "otp", "bkk", start, end, None, 1, None
        )
    )
    assert add_interaction.response.deferred is True
    assert "Added flight tracker **#1**" in add_interaction.followup.messages[0][0]
    assert provider.exact_calls
    assert db.get_user_flight_trackers(123)[0]["last_price"] == 599.0

    show_interaction = FakeInteraction(123)
    asyncio.run(group.get_command("show").callback(show_interaction))
    assert "#1 OTP -> BKK" in show_interaction.response.messages[0][0]
    assert "599.00 EUR" in show_interaction.response.messages[0][0]

    # Ownership is enforced by both command and SQL predicate.
    wrong_user = FakeInteraction(999)
    asyncio.run(group.get_command("delete").callback(wrong_user, 1))
    assert "not found" in wrong_user.response.messages[0][0].lower()

    delete_interaction = FakeInteraction(123)
    asyncio.run(group.get_command("delete").callback(delete_interaction, 1))
    assert "were deleted" in delete_interaction.response.messages[0][0]
    assert db.get_user_flight_trackers(123) == []
    asyncio.run(client.close())
