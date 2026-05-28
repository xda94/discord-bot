import logging
import random
import time

import discord
from discord import app_commands
from discord.ext import tasks

import db

logger = logging.getLogger("discord_bot")

INACTIVITY_THRESHOLD = 86400  # 24 hours, in seconds

INACTIVITY_MESSAGES = [
    "it's quiet... too quiet",
    "did everyone die?",
    "hello? is this thing on?",
    "I'm bored, someone say something",
    "*tumbleweed rolls by*",
    "this server is deader than my will to live",
    "I've been alone for 24 hours now. this is fine.",
    "not a single message in a whole day? wow.",
    "guess I'll just talk to myself then",
    "ce plm, ba? ati murit toti?",
    "HELLO? ANYBODY HERE? ECHOOOOO.....",
]


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
            for guild_id, guild_state in self._state.items():
                if now - guild_state["last_time"] >= INACTIVITY_THRESHOLD:
                    channel = self.client.get_channel(guild_state["channel_id"])
                    if channel:
                        msg = random.choice(INACTIVITY_MESSAGES)
                        await channel.send(msg)
                        guild_state["last_time"] = now
                        # Mirror the in-memory reset so a restart right after
                        # a nudge doesn't fire it again from the stale DB row.
                        db.set_guild_activity(guild_id, now, guild_state["channel_id"])
                        logger.info(f"Inactivity nudge sent in #{channel} (guild {guild_id})")
        except Exception:
            logger.exception("Error in check_inactivity loop")
