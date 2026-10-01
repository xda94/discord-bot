"""Mention-only natural command proposals and requester confirmations."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import discord

import db
from assistant_profiles import effective_profile
from features.flights import format_user_flight_trackers
from mention_utils import extract_mention_text
from natural_commands import (
    NaturalCommandError,
    ReminderAction,
    ShowFlightsAction,
    TrackURLAction,
    parse_natural_command,
)


class NaturalCommandConfirmView(discord.ui.View):
    """One-shot requester-only confirmation kept only in process memory."""

    def __init__(self, requester_id: int, execute, *, confirm_label: str = "Confirm"):
        super().__init__(timeout=120)
        self.requester_id = requester_id
        self._execute = execute
        # Python 3.9 binds Lock construction to the current event loop. Views
        # normally originate inside Discord's loop, but lazy construction also
        # keeps isolated tests and alternate callers safe.
        self._lock = None
        self.consumed = False
        self.message = None

        confirm = discord.ui.Button(label=confirm_label, style=discord.ButtonStyle.green)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        confirm.callback = self._confirm
        cancel.callback = self._cancel
        self.add_item(confirm)
        self.add_item(cancel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "Only the person who requested this action can use these buttons.",
            ephemeral=True,
        )
        return False

    async def _consume(self, interaction: discord.Interaction) -> bool:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self.consumed:
                await interaction.response.send_message(
                    "This proposal has already been handled.", ephemeral=True
                )
                return False
            self.consumed = True
            self.stop()
            for item in self.children:
                item.disabled = True
            return True

    async def _remove_buttons(self, interaction: discord.Interaction) -> None:
        message = getattr(interaction, "message", None) or self.message
        if message is not None:
            try:
                await message.edit(view=None)
            except (discord.HTTPException, discord.NotFound):
                pass

    async def _confirm(self, interaction: discord.Interaction) -> None:
        if not await self._consume(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await self._remove_buttons(interaction)
        try:
            messages = await self._execute(interaction)
        except Exception:
            messages = ["The confirmed action failed unexpectedly. Nothing else was queued."]
        if isinstance(messages, str):
            messages = [messages]
        for message in messages:
            await interaction.followup.send(message, ephemeral=True, suppress_embeds=True)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        if not await self._consume(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await self._remove_buttons(interaction)
        await interaction.followup.send("Cancelled. Nothing was changed.", ephemeral=True)

    async def on_timeout(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self.consumed:
                return
            self.consumed = True
            for item in self.children:
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except (discord.HTTPException, discord.NotFound):
                pass


class NaturalCommandsFeature:
    def __init__(self, client, *, bot_id: int, wishlist, flights, reminders):
        self.client = client
        self.bot_id = bot_id
        self.wishlist = wishlist
        self.flights = flights
        self.reminders = reminders

    async def _track(self, user_id: int, url: str):
        result = await self.wishlist.add_item_for_user(user_id, url)
        if result.status == "added":
            price = self.wishlist.converter.format_with_conversions(
                result.price, result.currency
            )
            stock = "unknown" if result.in_stock is None else (
                "in stock" if result.in_stock else "out of stock"
            )
            return f"✅ Added **{result.title or result.url}**. Price: `{price}`; stock: `{stock}`."
        return {
            "exists": "That URL is already in your tracking list.",
            "blocked": "The source blocked or timed out, so the URL was not added.",
            "unsupported": "The page had no supported price or stock data, so the URL was not added.",
            "database-error": "The page was read, but the database could not save it. Please try again.",
            "invalid": "That is not a valid HTTP(S) URL.",
        }.get(result.status, "The URL could not be added.")

    async def handle_message(self, message: discord.Message) -> bool:
        text = extract_mention_text(message, self.bot_id)
        if text is None:
            return False
        replied_text = ""
        resolved = getattr(getattr(message, "reference", None), "resolved", None)
        if resolved is not None:
            replied_text = (
                getattr(resolved, "clean_content", None)
                or getattr(resolved, "content", "")
            )
        parsed = parse_natural_command(text, replied_text=replied_text)
        if parsed is None:
            return False
        if isinstance(parsed, NaturalCommandError):
            await message.reply(
                parsed.message,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True

        profile = effective_profile(db.get_assistant_profile(message.author.id))
        if isinstance(parsed, TrackURLAction):
            proposal = f"Track this wishlist URL for **you**?\n`{parsed.url}`"

            async def execute(_interaction):
                return await self._track(message.author.id, parsed.url)

            label = "Confirm"
        elif isinstance(parsed, ShowFlightsAction):
            proposal = "Privately show the flight trackers saved for **you**?"

            async def execute(_interaction):
                return format_user_flight_trackers(message.author.id)

            label = "View my flights"
        else:
            remind_at = time.time() + parsed.seconds
            local = datetime.fromtimestamp(remind_at, ZoneInfo(profile.timezone))
            proposal = (
                "Create this self-reminder?\n"
                f"What: **{parsed.text}**\n"
                f"When: `{local.strftime('%Y-%m-%d %H:%M %Z')}` "
                f"(<t:{int(remind_at)}:F>, <t:{int(remind_at)}:R>)\n"
                f"Timezone: `{profile.timezone}`"
            )

            async def execute(_interaction):
                reminder_id = self.reminders.create_reminder_at(
                    message.author.id, message.channel.id, remind_at, parsed.text
                )
                if reminder_id is None:
                    return "The database could not save the reminder. Please try again."
                return f"✅ Reminder **#{reminder_id}** created for <t:{int(remind_at)}:R>."

            label = "Confirm"

        view = NaturalCommandConfirmView(message.author.id, execute, confirm_label=label)
        view.message = await message.reply(
            proposal,
            view=view,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
            suppress_embeds=True,
        )
        return True
