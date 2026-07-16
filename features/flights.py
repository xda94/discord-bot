"""Per-user flight price tracking commands and scheduled checks."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, timedelta

import discord
from discord import app_commands
from discord.ext import tasks

import db
from flight_provider import (
    AmadeusFlightProvider,
    FlightOffer,
    FlightProviderError,
    NoFlightOffers,
    SUPPORTED_CURRENCIES,
    build_date_windows,
    normalize_iata,
    parse_iso_date,
)


logger = logging.getLogger("discord_bot")

FLIGHT_CHECK_INTERVAL_HOURS = max(
    1.0, float(os.getenv("FLIGHT_CHECK_INTERVAL_HOURS", "5"))
)
FLIGHT_CHECK_GAP_SECONDS = max(
    0.0, float(os.getenv("FLIGHT_CHECK_GAP_SECONDS", "1"))
)
FLIGHT_CURRENCY_CHOICES = [
    app_commands.Choice(name=currency, value=currency)
    for currency in SUPPORTED_CURRENCIES
]


def validate_tracker_input(
    origin: str,
    destination: str,
    start_date: str,
    end_date: str,
    trip_days: int | None,
    adults: int,
    currency: str,
) -> dict:
    """Validate slash-command values and return their normalised DB form."""
    origin_code = normalize_iata(origin)
    destination_code = normalize_iata(destination)
    if origin_code == destination_code:
        raise ValueError("Origin and destination must be different.")

    start = parse_iso_date(start_date)
    end = parse_iso_date(end_date)
    if start < date.today():
        raise ValueError("The start date cannot be in the past.")
    if end <= start:
        raise ValueError("The end/return date must be after the start/departure date.")
    if not 1 <= int(adults) <= 9:
        raise ValueError("adults must be between 1 and 9.")

    currency_code = (currency or "EUR").upper()
    if currency_code not in SUPPORTED_CURRENCIES:
        raise ValueError(f"Currency must be one of: {', '.join(SUPPORTED_CURRENCIES)}.")

    stored_trip_days = int(trip_days or 0)
    if stored_trip_days:
        # Also proves the period can contain at least one complete window.
        build_date_windows(start.isoformat(), end.isoformat(), stored_trip_days)

    return {
        "origin": origin_code,
        "destination": destination_code,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "trip_days": stored_trip_days,
        "adults": int(adults),
        "currency": currency_code,
    }


def _format_offer(offer: FlightOffer) -> str:
    airlines = ", ".join(offer.airlines) if offer.airlines else "not provided"
    stops = "unknown"
    if offer.stops is not None:
        stops = "direct" if offer.stops == 0 else str(offer.stops)
    return (
        f"**{offer.total_price:.2f} {offer.currency}**\n"
        f"Dates: `{offer.departure_date}` -> `{offer.return_date}`\n"
        f"Airline code(s): `{airlines}` | Total stops: `{stops}`"
    )


def _format_tracker(tracker: dict) -> str:
    if tracker["trip_days"]:
        schedule = (
            f"flexible `{tracker['start_date']}` -> `{tracker['end_date']}`, "
            f"{tracker['trip_days']}-day sliding window"
        )
    else:
        schedule = f"exact `{tracker['start_date']}` -> `{tracker['end_date']}`"

    if tracker["last_price"] is None:
        price = "No price yet"
    else:
        price = (
            f"**{tracker['last_price']:.2f} {tracker['currency']}** for "
            f"`{tracker['last_departure_date']}` -> `{tracker['last_return_date']}`"
        )
    status = f"Last error: {tracker['last_error']}" if tracker["last_error"] else "Active"
    return (
        f"**#{tracker['id']} {tracker['origin']} -> {tracker['destination']}**\n"
        f"{schedule} | Adults: `{tracker['adults']}`\n"
        f"{price}\n{status}"
    )


class FlightTrackerFeature:
    """Own the /flight-tracker group and its five-hour background loop."""

    def __init__(
        self,
        client: discord.Client,
        tree: app_commands.CommandTree,
        provider: AmadeusFlightProvider | None = None,
    ):
        self.client = client
        self.tree = tree
        self.provider = provider or AmadeusFlightProvider()
        self._register_commands()

    async def start_tasks(self) -> None:
        if not self._check_loop.is_running():
            self._check_loop.start()

    def _register_commands(self) -> None:
        feature = self
        group = app_commands.Group(
            name="flight-tracker",
            description="Track round-trip flight prices per user",
        )

        @group.command(name="add", description="Add an exact or flexible flight price tracker")
        @app_commands.describe(
            origin="3-letter IATA code, e.g. OTP",
            destination="3-letter IATA code, e.g. BKK",
            start_date="Departure date, or first day of a flexible period (YYYY-MM-DD)",
            end_date="Return date, or last day of a flexible period (YYYY-MM-DD)",
            trip_days="Optional inclusive trip length; enables sliding windows (e.g. 10)",
            adults="Number of adult travellers (default 1)",
            currency="Price currency (default EUR)",
        )
        @app_commands.choices(currency=FLIGHT_CURRENCY_CHOICES)
        async def flight_add(
            interaction: discord.Interaction,
            origin: str,
            destination: str,
            start_date: str,
            end_date: str,
            trip_days: int | None = None,
            adults: int = 1,
            currency: app_commands.Choice[str] | None = None,
        ):
            await interaction.response.defer(ephemeral=True)
            currency_value = currency.value if currency else "EUR"
            try:
                values = validate_tracker_input(
                    origin, destination, start_date, end_date,
                    trip_days, adults, currency_value,
                )
            except ValueError as exc:
                await interaction.followup.send(f"Invalid tracker: {exc}", ephemeral=True)
                return

            tracker_id = db.add_flight_tracker(interaction.user.id, **values)
            if not tracker_id:
                await interaction.followup.send(
                    "That exact flight tracker is already in your list.", ephemeral=True
                )
                return

            tracker = db.get_flight_tracker(tracker_id, interaction.user.id)
            logger.info(
                f"Command /flight-tracker add by user {interaction.user.id}: "
                f"tracker {tracker_id} {values['origin']}-{values['destination']}"
            )
            try:
                offer = await feature._search_and_persist(tracker)
            except FlightProviderError as exc:
                await interaction.followup.send(
                    f"Tracker **#{tracker_id}** was added and will retry every "
                    f"{FLIGHT_CHECK_INTERVAL_HOURS:g} hours. Initial search: `{exc}`",
                    ephemeral=True,
                )
                return

            mode = (
                f"all {len(build_date_windows(values['start_date'], values['end_date'], values['trip_days']))} "
                f"sliding windows"
                if values["trip_days"]
                else "the exact dates"
            )
            await interaction.followup.send(
                f"Added flight tracker **#{tracker_id}** for **{values['origin']} -> "
                f"{values['destination']}** ({mode}).\nCurrent cheapest live offer:\n"
                f"{_format_offer(offer)}",
                ephemeral=True,
            )

        @group.command(name="show", description="Show your saved flight trackers")
        async def flight_show(interaction: discord.Interaction):
            trackers = db.get_user_flight_trackers(interaction.user.id)
            if not trackers:
                await interaction.response.send_message(
                    "You are not tracking any flights.", ephemeral=True
                )
                return

            blocks = [_format_tracker(tracker) for tracker in trackers]
            chunks = []
            current = "**Your flight trackers**\n\n"
            for block in blocks:
                if len(current) + len(block) + 2 > 1900:
                    chunks.append(current.rstrip())
                    current = block + "\n\n"
                else:
                    current += block + "\n\n"
            if current.strip():
                chunks.append(current.rstrip())

            await interaction.response.send_message(chunks[0], ephemeral=True)
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk, ephemeral=True)

        @group.command(name="delete", description="Delete one of your flight trackers")
        @app_commands.describe(tracker_id="Numeric ID shown by /flight-tracker show")
        async def flight_delete(interaction: discord.Interaction, tracker_id: int):
            if db.delete_flight_tracker(interaction.user.id, tracker_id):
                logger.info(
                    f"Command /flight-tracker delete by user {interaction.user.id}: "
                    f"tracker {tracker_id}"
                )
                await interaction.response.send_message(
                    f"Flight tracker **#{tracker_id}** and its price history were deleted.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "Tracker not found in your list.", ephemeral=True
                )

        self.tree.add_command(group)

    async def _search_and_persist(self, tracker: dict) -> FlightOffer:
        try:
            if tracker["trip_days"]:
                # As a flexible period progresses, discard departure windows
                # that are already in the past while keeping later windows active.
                effective_start = max(
                    parse_iso_date(tracker["start_date"]), date.today()
                ).isoformat()
                offer = await asyncio.to_thread(
                    self.provider.search_flexible,
                    tracker["origin"], tracker["destination"],
                    effective_start, tracker["end_date"],
                    tracker["trip_days"], tracker["adults"], tracker["currency"],
                )
            else:
                offer = await asyncio.to_thread(
                    self.provider.search_exact,
                    tracker["origin"], tracker["destination"],
                    tracker["start_date"], tracker["end_date"],
                    tracker["adults"], tracker["currency"],
                )
        except FlightProviderError as exc:
            db.update_flight_tracker_result(tracker["id"], error=str(exc)[:500])
            raise

        db.add_flight_price_history(
            tracker["id"], offer.total_price, offer.currency,
            offer.departure_date, offer.return_date,
        )
        db.update_flight_tracker_result(
            tracker["id"], offer.total_price, offer.currency,
            offer.departure_date, offer.return_date, error=None,
        )
        return offer

    async def _process_tracker(self, tracker: dict) -> None:
        # Keep completed trips in `/flight-tracker show` until the owner
        # deletes them, but stop spending provider quota on dates that passed.
        latest_departure = (
            parse_iso_date(tracker["end_date"])
            - timedelta(days=tracker["trip_days"] - 1)
            if tracker["trip_days"]
            else parse_iso_date(tracker["start_date"])
        )
        if latest_departure < date.today():
            db.update_flight_tracker_result(
                tracker["id"], error="Tracking period has ended; delete this tracker when done."
            )
            return
        old_price = tracker["last_price"]
        try:
            offer = await self._search_and_persist(tracker)
        except NoFlightOffers as exc:
            logger.info(f"No offer for flight tracker {tracker['id']}: {exc}")
            return
        except FlightProviderError as exc:
            logger.warning(f"Flight tracker {tracker['id']} check failed: {exc}")
            return

        # A first successful result after previous failures is useful, as is a
        # strict price drop. Equal/higher prices stay quiet to avoid DM spam.
        if old_price is not None and offer.total_price >= old_price:
            return
        try:
            user = await self.client.fetch_user(tracker["user_id"])
            if user:
                label = "First price found" if old_price is None else (
                    f"Price dropped from {old_price:.2f} {tracker['currency']}"
                )
                await user.send(
                    f"Flight tracker **#{tracker['id']}**: **{tracker['origin']} -> "
                    f"{tracker['destination']}**\n{label}.\n{_format_offer(offer)}"
                )
        except Exception as exc:
            logger.error(
                f"Could not send flight tracker DM to user {tracker['user_id']}: {exc}"
            )

    @tasks.loop(hours=FLIGHT_CHECK_INTERVAL_HOURS)
    async def _check_loop(self):
        trackers = db.get_all_flight_trackers()
        logger.info(f"Starting scheduled flight check for {len(trackers)} tracker(s)")
        for tracker in trackers:
            try:
                await self._process_tracker(tracker)
            except Exception:
                logger.exception(f"Unexpected error checking flight tracker {tracker['id']}")
            if FLIGHT_CHECK_GAP_SECONDS:
                await asyncio.sleep(FLIGHT_CHECK_GAP_SECONDS)
