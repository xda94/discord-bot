"""Per-user flight price tracking commands and scheduled checks."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import date

import discord
from discord import app_commands
from discord.ext import tasks

import db
from flight_provider import (
    FlightOffer,
    FlightProviderError,
    NoFlightOffers,
    SerpApiFlightProvider,
    SUPPORTED_CURRENCIES,
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

    return {
        "origin": origin_code,
        "destination": destination_code,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "trip_days": 0,
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
        f"Airline(s): `{airlines}` | Outbound stops: `{stops}`"
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


def _select_trackers_for_pass(trackers: list[dict]) -> list[dict]:
    """Choose at most one least-recently checked tracker per API-key owner.

    At a five-hour cadence this caps scheduled SerpApi usage at about 144
    searches per user in a 30-day month, leaving headroom inside the 250-search
    free plan for immediate searches when trackers are added.
    """
    selected: dict[int, dict] = {}
    for tracker in trackers:
        user_id = tracker["user_id"]
        current = selected.get(user_id)
        checked_at = tracker["last_checked_at"]
        current_checked_at = current["last_checked_at"] if current else None
        candidate_key = (checked_at is not None, checked_at or 0.0, tracker["id"])
        current_key = (
            current_checked_at is not None,
            current_checked_at or 0.0,
            current["id"],
        ) if current else None
        if current_key is None or candidate_key < current_key:
            selected[user_id] = tracker
    return list(selected.values())


class _FlightCredentialsModal(discord.ui.Modal, title="Flight Tracker Login"):
    api_key = discord.ui.TextInput(
        label="SerpApi API Key",
        placeholder="Copy it from your SerpApi dashboard",
        min_length=1,
        max_length=200,
    )

    def __init__(
        self,
        feature: "FlightTrackerFeature",
        pending_tracker: dict | None = None,
    ):
        super().__init__()
        self._feature = feature
        self._pending_tracker = pending_tracker

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        api_key = self.api_key.value.strip()
        provider = self._feature._build_provider(api_key)
        try:
            await asyncio.to_thread(provider.validate_credentials)
        except FlightProviderError as exc:
            await interaction.followup.send(
                f"SerpApi rejected that key. Nothing was saved: `{exc}`",
                ephemeral=True,
            )
            return

        if not db.set_flight_api_credentials(interaction.user.id, api_key):
            await interaction.followup.send(
                "The credentials were valid, but the database could not save them.",
                ephemeral=True,
            )
            return

        self._feature._remember_provider(
            interaction.user.id, api_key, provider
        )
        logger.info(
            f"SerpApi key validated and stored for user {interaction.user.id}"
        )
        if self._pending_tracker is not None:
            await self._feature._add_tracker(interaction, self._pending_tracker)
        else:
            await interaction.followup.send(
                "SerpApi login saved. Future flight searches will use your own account.",
                ephemeral=True,
            )


class FlightTrackerFeature:
    """Own the /flight_tracker_* commands and five-hour background loop."""

    def __init__(
        self,
        client: discord.Client,
        tree: app_commands.CommandTree,
        provider_factory=None,
    ):
        self.client = client
        self.tree = tree
        self._provider_factory = provider_factory or (
            lambda api_key: SerpApiFlightProvider(api_key=api_key)
        )
        # Reuse HTTP sessions across one user's trackers. Cache entries are
        # invalidated whenever that user logs in again or logs out.
        self._providers: dict[int, tuple[str, SerpApiFlightProvider]] = {}
        self._register_commands()

    async def start_tasks(self) -> None:
        if not self._check_loop.is_running():
            self._check_loop.start()

    def _register_commands(self) -> None:
        feature = self

        @self.tree.command(
            name="flight_tracker_add",
            description="Add a fixed-date round-trip flight price tracker",
        )
        @app_commands.describe(
            origin="3-letter IATA code, e.g. OTP",
            destination="3-letter IATA code, e.g. BKK",
            start_date="Exact departure date (YYYY-MM-DD)",
            end_date="Exact return date (YYYY-MM-DD)",
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
            adults: int = 1,
            currency: app_commands.Choice[str] | None = None,
        ):
            currency_value = currency.value if currency else "EUR"
            try:
                values = validate_tracker_input(
                    origin, destination, start_date, end_date,
                    adults, currency_value,
                )
            except ValueError as exc:
                await interaction.response.send_message(
                    f"Invalid tracker: {exc}", ephemeral=True
                )
                return

            if db.get_flight_api_credentials(interaction.user.id) is None:
                await interaction.response.send_modal(
                    _FlightCredentialsModal(feature, pending_tracker=values)
                )
                return

            await interaction.response.defer(ephemeral=True)
            await feature._add_tracker(interaction, values)

        @self.tree.command(
            name="flight_tracker_show", description="Show your saved flight trackers"
        )
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

        @self.tree.command(
            name="flight_tracker_delete", description="Delete one of your flight trackers"
        )
        @app_commands.describe(tracker_id="Numeric ID shown by /flight_tracker_show")
        async def flight_delete(interaction: discord.Interaction, tracker_id: int):
            if db.delete_flight_tracker(interaction.user.id, tracker_id):
                logger.info(
                    f"Command /flight_tracker_delete by user {interaction.user.id}: "
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

        @self.tree.command(
            name="flight_tracker_login",
            description="Set or replace your private SerpApi API key",
        )
        async def flight_login(interaction: discord.Interaction):
            await interaction.response.send_modal(_FlightCredentialsModal(feature))

        @self.tree.command(
            name="flight_tracker_logout",
            description="Remove your saved SerpApi API key",
        )
        async def flight_logout(interaction: discord.Interaction):
            removed = db.delete_flight_api_credentials(interaction.user.id)
            feature._forget_provider(interaction.user.id)
            message = (
                "Your SerpApi login was removed. Existing trackers are paused until you "
                "run `/flight_tracker_login` or `/flight_tracker_add` and log in again."
                if removed
                else "You do not have a SerpApi login saved."
            )
            await interaction.response.send_message(message, ephemeral=True)

    def _build_provider(self, api_key: str):
        return self._provider_factory(api_key)

    def _forget_provider(self, user_id: int) -> None:
        self._providers.pop(user_id, None)

    def _remember_provider(
        self, user_id: int, api_key: str, provider
    ) -> None:
        self._providers[user_id] = (api_key, provider)

    def _provider_for_user(self, user_id: int):
        credentials = db.get_flight_api_credentials(user_id)
        if credentials is None:
            raise FlightProviderError(
                "No SerpApi login is saved. Run /flight_tracker_login first."
            )
        api_key = credentials["api_key"]
        cached = self._providers.get(user_id)
        if cached and cached[0] == api_key:
            return cached[1]
        provider = self._build_provider(api_key)
        self._providers[user_id] = (api_key, provider)
        return provider

    async def _add_tracker(self, interaction: discord.Interaction, values: dict) -> None:
        tracker_id = db.add_flight_tracker(interaction.user.id, **values)
        if not tracker_id:
            await interaction.followup.send(
                "That exact flight tracker is already in your list.", ephemeral=True
            )
            return

        tracker = db.get_flight_tracker(tracker_id, interaction.user.id)
        logger.info(
            f"Command /flight_tracker_add by user {interaction.user.id}: "
            f"tracker {tracker_id} {values['origin']}-{values['destination']}"
        )
        try:
            offer = await self._search_and_persist(tracker)
        except FlightProviderError as exc:
            await interaction.followup.send(
                f"Tracker **#{tracker_id}** was added and will retry every "
                f"{FLIGHT_CHECK_INTERVAL_HOURS:g} hours. Initial search: `{exc}`",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Added flight tracker **#{tracker_id}** for **{values['origin']} -> "
            f"{values['destination']}** (fixed dates).\nCurrent cheapest Google Flights offer:\n"
            f"{_format_offer(offer)}",
            ephemeral=True,
        )

    async def _search_and_persist(self, tracker: dict) -> FlightOffer:
        try:
            if tracker["trip_days"]:
                raise FlightProviderError(
                    "Flexible-date tracking is no longer supported. Delete this tracker "
                    "and add one with fixed departure and return dates."
                )
            provider = self._provider_for_user(tracker["user_id"])
            offer = await asyncio.to_thread(
                provider.search_exact,
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
        # Keep completed trips in `/flight_tracker_show` until the owner
        # deletes them, but stop spending provider quota on dates that passed.
        if (
            tracker["last_checked_at"] is not None
            and time.time() - tracker["last_checked_at"]
            < FLIGHT_CHECK_INTERVAL_HOURS * 3600
        ):
            # `tasks.loop` runs once immediately after every bot restart. This
            # guard prevents restarts from consuming searches ahead of cadence.
            return
        if tracker["trip_days"]:
            db.update_flight_tracker_result(
                tracker["id"],
                error=(
                    "Flexible-date tracking was retired to protect the SerpApi quota; "
                    "delete this tracker and add fixed dates."
                ),
            )
            return
        latest_departure = parse_iso_date(tracker["start_date"])
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
        all_trackers = db.get_all_flight_trackers()
        trackers = _select_trackers_for_pass(all_trackers)
        logger.info(
            f"Starting scheduled flight check for {len(trackers)} tracker(s) "
            f"selected from {len(all_trackers)} total (maximum one per user)"
        )
        for tracker in trackers:
            try:
                await self._process_tracker(tracker)
            except Exception:
                logger.exception(f"Unexpected error checking flight tracker {tracker['id']}")
            if FLIGHT_CHECK_GAP_SECONDS:
                await asyncio.sleep(FLIGHT_CHECK_GAP_SECONDS)
