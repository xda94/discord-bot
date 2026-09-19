from __future__ import annotations

import asyncio
import logging
import random
import time

import discord
from discord import app_commands
from discord.ext import tasks

import db
from analytics import record, record_for
from mention_utils import resolve_bot_display_name
from tease_llm import generate_inactivity_message

logger = logging.getLogger("discord_bot")

# How many recent messages to scan for someone to tag.
INACTIVITY_HISTORY_LOOKBACK = 50

INACTIVITY_THRESHOLD = 86400  # 24 hours, in seconds


def pick_chatter(messages: list[discord.Message]) -> discord.abc.User | None:
    """Pick a random non-bot author from `messages`, or None if there are none.

    Deduplicates by user id so someone who sent many messages isn't favoured."""
    authors = {m.author.id: m.author for m in messages if not m.author.bot}
    if not authors:
        return None
    return random.choice(list(authors.values()))


class InactivityFeature:
    """Tracks the most recent activity per guild and nudges silent channels."""

    def __init__(self, client: discord.Client, tree: app_commands.CommandTree):
        self.client = client
        self.tree = tree
        # `_state` is the hot in-memory cache (written on every message), but
        # the source of truth lives in the `guild_activity` table — without
        # that, a restart resets every guild's clock and a quiet server has
        # to wait another 24h before the next nudge.
        self._state: dict[int, dict] = {
            guild_id: {"last_time": last_time, "channel_id": channel_id}
            for guild_id, last_time, channel_id in db.get_all_guild_activity()
        }
        if self._state:
            logger.info(f"Restored activity timestamps for {len(self._state)} guild(s)")
        self._register_commands()

    def _register_commands(self) -> None:
        @self.tree.command(
            name="llm-inactivity",
            description="Activate or deactivate LLM inactivity nudges in this server",
        )
        @app_commands.describe(action="Whether to activate or deactivate the nudges")
        @app_commands.choices(
            action=[
                app_commands.Choice(name="Activate", value="activate"),
                app_commands.Choice(name="Deactivate", value="deactivate"),
            ]
        )
        @app_commands.default_permissions(manage_guild=True)
        async def llm_inactivity(
            interaction: discord.Interaction,
            action: str,
        ):
            logger.info(
                f"Command /llm-inactivity called by {interaction.user} "
                f"in guild {interaction.guild_id} with action {action}"
            )
            if interaction.guild_id is None:
                await interaction.response.send_message(
                    "This command must be run inside a server, not in DMs.",
                    ephemeral=True,
                )
                return

            permissions = getattr(interaction.user, "guild_permissions", None)
            can_manage = permissions and (
                permissions.manage_guild or permissions.administrator
            )
            if not can_manage:
                await interaction.response.send_message(
                    "You need the **Manage Server** permission to change this setting.",
                    ephemeral=True,
                )
                return

            enabled = action == "activate"
            db.set_guild_inactivity_enabled(interaction.guild_id, enabled)
            state = "activated" if enabled else "deactivated"
            await interaction.response.send_message(
                f"LLM inactivity nudges are now **{state}** for this server.",
                ephemeral=True,
            )

    async def handle_message(self, message: discord.Message) -> bool:
        if message.guild:
            now = time.time()
            self._state[message.guild.id] = {
                "last_time": now,
                "channel_id": message.channel.id,
            }
            # Cheap UPSERT on the hot path: every guild message goes through
            # this. SQLite handles ~10k of these per second on a Pi, well
            # above any Discord guild's chat rate.
            db.set_guild_activity(message.guild.id, now, message.channel.id)
        return False

    async def start_tasks(self) -> None:
        if not self._check.is_running():
            self._check.start()

    @tasks.loop(minutes=30)
    async def _check(self):
        try:
            now = time.time()
            # Snapshot: _send_nudge awaits, during which handle_message may add
            # a new guild to _state — iterating the live dict would then raise.
            for guild_id, guild_state in list(self._state.items()):
                if not db.is_guild_inactivity_enabled(guild_id):
                    continue
                if now - guild_state["last_time"] < INACTIVITY_THRESHOLD:
                    continue
                channel = self.client.get_channel(guild_state["channel_id"])
                if channel is None:
                    continue
                if not await self._send_nudge(channel):
                    # Keep the guild overdue so the next scheduled check can
                    # retry once llama.cpp is available again.
                    continue
                await record("scheduled", "inactivity-nudge", guild_id=guild_id)
                guild_state["last_time"] = now
                # Mirror the in-memory reset so a restart right after a nudge
                # doesn't fire it again from the stale DB row.
                db.set_guild_activity(guild_id, now, guild_state["channel_id"])
                logger.info(f"Inactivity nudge sent in #{channel} (guild {guild_id})")
        except Exception:
            logger.exception("Error in check_inactivity loop")

    async def _send_nudge(self, channel: discord.abc.Messageable) -> bool:
        """Post an LLM-generated nudge and report whether it was sent."""
        target = await self._pick_recent_chatter(channel)
        bot_name = resolve_bot_display_name(getattr(channel, "guild", None), self.client)
        # llama-server inference is blocking (and the model may be busy), so keep it off
        # the event loop.
        text = await asyncio.to_thread(
            generate_inactivity_message,
            bot_name,
            ask_question=target is not None,
        )
        if text is None:
            logger.warning("Skipping inactivity nudge because LLM generation failed")
            await record_for("failure", "inactivity-nudge", channel)
            return False
        try:
            if target is not None:
                await channel.send(f"<@{target.id}> {text}")
            else:
                await channel.send(text)
        except Exception:
            await record_for("failure", "inactivity-nudge", channel)
            raise
        return True

    async def _pick_recent_chatter(
        self, channel: discord.abc.Messageable
    ) -> discord.abc.User | None:
        try:
            messages = [
                m async for m in channel.history(limit=INACTIVITY_HISTORY_LOOKBACK)
            ]
        except discord.DiscordException:
            logger.exception("Could not read history for inactivity nudge")
            return None
        return pick_chatter(messages)
