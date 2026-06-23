from __future__ import annotations

import asyncio
import logging
import random
from datetime import date

import discord
from discord import app_commands

from tease_llm import enhance_tease

logger = logging.getLogger("discord_bot")
TEASE_BASE_CHANCE = 0.10

TEASE_MOODS = ["bad", "good", "computer", "gen-z", "dad", "anime", "shy", "lenghel"]

MOOD_CHOICES = [
    app_commands.Choice(name=m, value=m) for m in TEASE_MOODS
] + [app_commands.Choice(name="random", value="random")]

class TeasesFeature:
    """Random teases that fire on messages, plus the /mood command."""

    def __init__(self, client: discord.Client, tree: app_commands.CommandTree):
        self.client = client
        self.tree = tree
        self.current_mood = random.choice(TEASE_MOODS)
        logger.info(f"Tease mood initialised to '{self.current_mood}'")
        self.teases_today = 0
        self.tease_reset_date: date | None = None
        self._register_commands()

    async def handle_message(self, message: discord.Message) -> bool:
        today = date.today()
        if self.tease_reset_date != today:
            self.teases_today = 0
            self.tease_reset_date = today

        chance = TEASE_BASE_CHANCE / (1 + self.teases_today)
        if random.random() >= chance:
            return False

        tease = await asyncio.to_thread(
            enhance_tease,
            self.current_mood,
            message.author.display_name,
            message.content,
        )
        if not tease:
            return False

        try:
            await message.reply(tease, mention_author=False)
        except Exception:
            logger.exception("Failed to send tease")
            return False

        self.teases_today += 1
        logger.info(
            f"Tease #{self.teases_today} triggered on {message.author} in #{message.channel}"
        )
        return False

    def _register_commands(self) -> None:
        feature = self

        @self.tree.command(name="mood", description="Set the bot's mood")
        @app_commands.describe(mood="The mood to set")
        @app_commands.choices(mood=MOOD_CHOICES)
        async def mood(interaction: discord.Interaction, mood: app_commands.Choice[str]):
            chosen = mood.value
            if chosen == "random":
                chosen = random.choice(TEASE_MOODS)
            logger.info(
                f"Command /mood called by {interaction.user} — setting mood to {chosen}"
            )
            feature.current_mood = chosen
            feature.teases_today = 0
            await interaction.response.send_message(f"Mood set to **{mood.value}**.")
