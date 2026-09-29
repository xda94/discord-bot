from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from datetime import date as date_class
from datetime import datetime, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks

import db
from analytics import record, record_for
from llm.responses import generate_birthday_message

logger = logging.getLogger("discord_bot")

_BIRTHDAY_PATTERN = re.compile(r"\s*(\d{1,2})([.\-/])(\d{1,2})\s*")
_BIRTHDAY_CHECK_INTERVAL = timedelta(minutes=30)


def _parse_birthday(value: str) -> Optional[tuple[int, int]]:
    """Parse a day-first birthday and return ``(month, day)``."""
    if not isinstance(value, str):
        return None
    match = _BIRTHDAY_PATTERN.fullmatch(value)
    if match is None:
        return None
    day, separator, month = match.groups()
    if separator not in {".", "-", "/"}:
        return None
    day_number = int(day)
    month_number = int(month)
    try:
        # 2000 is a leap year, so February 29 remains a valid registration.
        date_class(2000, month_number, day_number)
    except ValueError:
        return None
    return month_number, day_number


def _next_birthday_check(now: datetime) -> datetime:
    """Return the next local 00:01-anchored 30-minute check time."""
    anchor = now.replace(hour=0, minute=1, second=0, microsecond=0)
    if now < anchor:
        return anchor

    elapsed = now - anchor
    completed_intervals = elapsed // _BIRTHDAY_CHECK_INTERVAL
    candidate = anchor + completed_intervals * _BIRTHDAY_CHECK_INTERVAL
    if candidate <= now < candidate + timedelta(minutes=1):
        return now
    return candidate + _BIRTHDAY_CHECK_INTERVAL


def _registration_values(row):
    """Return the stable birthday tuple fields from a database row."""
    if row is None:
        return None
    if isinstance(row, dict):
        return (
            row["channel_id"],
            row["guild_id"],
            row["month"],
            row["day"],
            row["revision"],
            row["last_sent_year"],
        )
    return tuple(row)


class BirthdaysFeature:
    """Per-user annual birthday registrations and scheduled greetings."""

    def __init__(self, client: discord.Client, tree: app_commands.CommandTree):
        self.client = client
        self.tree = tree
        self._register_commands()

    def _register_commands(self) -> None:
        @self.tree.command(
            name="set-birthday",
            description="Set your birthday or remove your saved birthday",
        )
        @app_commands.describe(
            date="Birthday as DD.MM, DD-MM, or DD/MM; omit to remove it"
        )
        async def set_birthday(
            interaction: discord.Interaction, date: Optional[str] = None
        ):
            user_id = interaction.user.id
            if date is None:
                try:
                    deleted = db.delete_birthday(user_id)
                except sqlite3.Error:
                    logger.exception(
                        "Could not delete birthday registration for user=%s",
                        user_id,
                    )
                    await record_for("failure", "command/set-birthday", interaction)
                    await interaction.response.send_message(
                        "I couldn't update your birthday right now. Please try again.",
                        ephemeral=True,
                    )
                    return
                message = (
                    "Your saved birthday was removed."
                    if deleted
                    else "You do not have a saved birthday."
                )
                await interaction.response.send_message(message, ephemeral=True)
                return

            parsed = _parse_birthday(date)
            if parsed is None:
                await interaction.response.send_message(
                    "Invalid birthday. Use a real day-first date such as `25.12`, `25-12`, or `25/12`.",
                    ephemeral=True,
                )
                return

            channel_id = interaction.channel_id
            if channel_id is None:
                await interaction.response.send_message(
                    "I need a channel destination to save a birthday here.",
                    ephemeral=True,
                )
                return

            month, day = parsed
            guild_id = interaction.guild_id
            try:
                db.set_birthday(user_id, channel_id, guild_id, month, day)
            except sqlite3.Error:
                # Keep the supplied date out of logs and analytics details.
                logger.exception(
                    "Could not save birthday registration for user=%s", user_id
                )
                await record_for("failure", "command/set-birthday", interaction)
                await interaction.response.send_message(
                    "I couldn't update your birthday right now. Please try again.",
                    ephemeral=True,
                )
                return

            destination = (
                f"<#{channel_id}>" if guild_id is not None else "this DM"
            )
            await interaction.response.send_message(
                f"Saved your birthday as **{day:02d}.{month:02d}** for an annual greeting in {destination}. "
                "The first attempt is at 00:01 using the bot's local date; failures retry every 30 minutes that day. "
                "No belated message is sent after the date. "
                "February 29 is celebrated only in leap years.",
                ephemeral=True,
            )

    async def start_tasks(self) -> None:
        if not self._check.is_running():
            self._check.start()

    @tasks.loop(minutes=30)
    async def _check(self):
        today = date_class.today()
        try:
            due = db.get_due_birthdays(today)
        except Exception:
            logger.exception("Could not list due birthdays")
            await record("failure", "birthday-check", scope_type="global")
            return

        for birthday in due:
            try:
                await self._send_birthday(birthday, today)
            except Exception:
                logger.exception("Unexpected error while checking one birthday")
                try:
                    guild_id = birthday[2]
                except (IndexError, TypeError):
                    guild_id = None
                await self._record_delivery_failure(guild_id)

    @_check.before_loop
    async def _wait_until_first_check(self) -> None:
        await self.client.wait_until_ready()
        now = datetime.now()
        next_check = _next_birthday_check(now)
        delay = (next_check - now).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)

    @staticmethod
    def _scope_failure_kwargs(guild_id: int | None) -> dict:
        return {"guild_id": guild_id} if guild_id is not None else {"scope_type": "dm"}

    async def _record_delivery_failure(self, guild_id: int | None) -> None:
        await record(
            "failure",
            "birthday-delivery",
            **self._scope_failure_kwargs(guild_id),
        )

    async def _record_delivery_success(self, guild_id: int | None) -> None:
        await record(
            "scheduled",
            "birthday-delivery",
            **self._scope_failure_kwargs(guild_id),
        )

    @staticmethod
    def _is_current_registration(
        row,
        *,
        user_id: int,
        revision: str,
        today: date_class,
    ) -> bool:
        values = _registration_values(row)
        if values is None:
            return False
        _channel_id, _guild_id, month, day, current_revision, last_sent_year = values
        return (
            current_revision == revision
            and month == today.month
            and day == today.day
            and (last_sent_year is None or last_sent_year < today.year)
        )

    @staticmethod
    def _channel_guild_id(channel) -> int | None:
        guild = getattr(channel, "guild", None)
        actual_guild_id = getattr(guild, "id", None) if guild is not None else None
        if actual_guild_id is None:
            actual_guild_id = getattr(channel, "guild_id", None)
        return actual_guild_id if isinstance(actual_guild_id, int) else None

    def _channel_is_sendable(self, channel, guild_id: int | None) -> bool:
        if not callable(getattr(channel, "send", None)):
            return False
        if self._channel_guild_id(channel) != guild_id:
            return False

        if guild_id is not None:
            permissions_for = getattr(channel, "permissions_for", None)
            bot_user = getattr(self.client, "user", None)
            if callable(permissions_for) and bot_user is not None:
                permissions = permissions_for(bot_user)
                can_send = getattr(permissions, "send_messages", None)
                if can_send is False:
                    return False
        return True

    async def _resolve_channel(self, channel_id: int, guild_id: int | None):
        channel = self.client.get_channel(channel_id)
        if channel is None:
            channel = await self.client.fetch_channel(channel_id)
        return channel if self._channel_is_sendable(channel, guild_id) else None

    async def _send_birthday(self, birthday, today: date_class) -> None:
        saved_guild_id = None

        try:
            user_id, _channel_id, saved_guild_id, _month, _day, revision, _last_sent = birthday
            current = db.get_birthday(user_id)
            if not self._is_current_registration(
                current, user_id=user_id, revision=revision, today=today
            ):
                return

            current_values = _registration_values(current)
            channel_id = current_values[0]
            saved_guild_id = current_values[1]
            channel = await self._resolve_channel(channel_id, saved_guild_id)
            if channel is None:
                logger.warning(
                    "Birthday destination unavailable for user=%s", user_id
                )
                await self._record_delivery_failure(saved_guild_id)
                return

            text = await asyncio.to_thread(generate_birthday_message)
            if not text or not text.strip():
                logger.warning("Birthday greeting generation returned no message")
                await self._record_delivery_failure(saved_guild_id)
                return

            current = db.get_birthday(user_id)
            if not self._is_current_registration(
                current, user_id=user_id, revision=revision, today=today
            ):
                return
            current_values = _registration_values(current)
            if not self._channel_is_sendable(channel, current_values[1]):
                await self._record_delivery_failure(current_values[1])
                return

            await channel.send(
                f"🎂 <@{user_id}> {text}",
                allowed_mentions=discord.AllowedMentions(
                    users=[discord.Object(id=user_id)],
                    roles=False,
                    everyone=False,
                    replied_user=False,
                ),
                suppress_embeds=True,
            )
        except Exception:
            logger.exception("Birthday delivery failed for user=%s", user_id)
            await self._record_delivery_failure(saved_guild_id)
            return

        try:
            marked = db.mark_birthday_sent(user_id, revision, today.year)
        except Exception:
            logger.exception("Could not mark birthday sent for user=%s", user_id)
            await record(
                "failure",
                "birthday-state",
                **self._scope_failure_kwargs(saved_guild_id),
            )
        else:
            if not marked:
                logger.warning("Birthday marker was not advanced for user=%s", user_id)
                await record(
                    "failure",
                    "birthday-state",
                    **self._scope_failure_kwargs(saved_guild_id),
                )

        await self._record_delivery_success(saved_guild_id)
