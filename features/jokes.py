import logging
from datetime import date, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import tasks

import db

logger = logging.getLogger("discord_bot")


class JokesFeature:
    """Daily joke loop plus /joke_add and /joke_activation commands."""

    def __init__(self, client: discord.Client, tree: app_commands.CommandTree):
        self.client = client
        self.tree = tree

        settings = db.get_joke_settings()
        self.channel_id: int | None = settings["channel_id"]
        self.send_time: str = settings["send_time"]

        last_sent = db.get_setting("joke_last_sent_date")
        self.last_sent_date: date | None = date.fromisoformat(last_sent) if last_sent else None

        self._register_commands()

    async def start_tasks(self) -> None:
        if not self._check.is_running():
            self._check.start()

    def _register_commands(self) -> None:
        feature = self

        @self.tree.command(name="joke_add", description="Add a joke/text to the daily joke list")
        @app_commands.describe(text="The joke or text to add")
        async def joke_add(interaction: discord.Interaction, text: str):
            logger.info(f"Command /joke_add called by {interaction.user}")
            db.add_joke(text)
            await interaction.response.send_message("Joke added!")

        @self.tree.command(
            name="joke_activation",
            description="Activate the daily joke in this channel at a specific time",
        )
        @app_commands.describe(time="The time to send the daily joke (e.g. 14:00)")
        async def joke_activation(interaction: discord.Interaction, time: str):
            logger.info(f"Command /joke_activation called by {interaction.user} with time {time}")
            try:
                datetime.strptime(time, "%H:%M")
            except ValueError:
                await interaction.response.send_message(
                    "Invalid time format! Use HH:MM (e.g. `14:00`).", ephemeral=True
                )
                return

            feature.send_time = time
            feature.channel_id = interaction.channel_id
            db.set_setting("joke_send_time", time)
            db.set_setting("joke_channel_id", interaction.channel_id)
            await interaction.response.send_message(
                f"Daily joke activated in this channel at **{time}** every day."
            )

    @tasks.loop(seconds=30)
    async def _check(self):
        try:
            now = datetime.now()
            today = now.date()
            target = datetime.strptime(self.send_time, "%H:%M").replace(
                year=now.year, month=now.month, day=now.day
            )

            if self.last_sent_date == today:
                return
            if not (target <= now <= target + timedelta(minutes=2)):
                return
            if self.channel_id is None:
                logger.warning("No joke channel set. Use /joke_activation to set one.")
                return

            result = db.get_unsent_joke()
            if result is None:
                db.reset_jokes()
                result = db.get_unsent_joke()
                if result is None:
                    logger.info("No jokes in database. Skipping daily joke.")
                    return

            joke_id, text = result
            channel = self.client.get_channel(self.channel_id)
            if channel:
                await channel.send(f"**Joke of the day:**\n{text}", suppress_embeds=True)
                db.mark_joke_sent(joke_id)
                self.last_sent_date = today
                db.set_setting("joke_last_sent_date", today.isoformat())
                logger.info(f"Daily joke sent: ID {joke_id}")
            else:
                logger.warning(f"Joke channel {self.channel_id} not accessible")
        except Exception:
            logger.exception("Error in daily_joke_check task")
