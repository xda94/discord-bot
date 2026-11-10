from __future__ import annotations

import asyncio
import logging
import random
from datetime import date

import discord
from discord import app_commands

from analytics import record_for
from llm.responses import enhance_tease

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
        self._guild_state: dict[int, dict] = {}
        self._register_commands()

    def _state_for(self, guild_id: int) -> dict:
        state = self._guild_state.get(guild_id)
        if state is None:
            state = {
                "mood": random.choice(TEASE_MOODS),
                "teases_today": 0,
                "reset_date": None,
            }
            self._guild_state[guild_id] = state
            logger.info("Tease mood initialised to '%s' for guild %s", state["mood"], guild_id)
        return state

    async def handle_message(self, message: discord.Message) -> bool:
        if not message.guild:
            return False

        state = self._state_for(message.guild.id)
        today = date.today()
        if state["reset_date"] != today:
            state["teases_today"] = 0
            state["reset_date"] = today

        chance = TEASE_BASE_CHANCE / (1 + state["teases_today"])
        if random.random() >= chance:
            return False

        tease = await asyncio.to_thread(
            enhance_tease,
            state["mood"],
            message.author.display_name,
            message.content,
        )
        if not tease:
            return False

        try:
            await message.reply(tease, mention_author=False)
        except Exception:
            logger.exception("Failed to send tease")
            await record_for("failure", "tease-reply", message)
            return False

        state["teases_today"] += 1
        await record_for("automatic", "tease-reply", message)
        logger.info(
            "Tease #%s triggered on %s in #%s",
            state["teases_today"],
            message.author,
            message.channel,
        )
        return False

    def _register_commands(self) -> None:
        feature = self

        @self.tree.command(name="mood", description="Set the bot's mood")
        @app_commands.describe(mood="The mood to set")
        @app_commands.choices(mood=MOOD_CHOICES)
        async def mood(interaction: discord.Interaction, mood: app_commands.Choice[str]):
            if interaction.guild_id is None:
                await interaction.response.send_message(
                    "This command must be run inside a server, not in DMs.",
                    ephemeral=True,
                )
                return
            chosen = mood.value
            if chosen == "random":
                chosen = random.choice(TEASE_MOODS)
            logger.info(
                "Command /mood called by %s — setting mood to %s",
                interaction.user,
                chosen,
            )
            state = feature._state_for(interaction.guild_id)
            state["mood"] = chosen
            state["teases_today"] = 0
            await interaction.response.send_message(f"Mood set to **{mood.value}**.")
