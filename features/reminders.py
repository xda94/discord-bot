import logging
import re
import time

import discord
from discord import app_commands
from discord.ext import tasks

import db

logger = logging.getLogger("discord_bot")


def _parse_time(time_str: str) -> int | None:
    """Parse a duration string like '30m', '2h', '1d' into seconds."""
    minutes_per_unit = {"m": 1, "h": 60, "d": 1440}
    match = re.match(r"(\d+)([mhd])", time_str.lower())
    if not match:
        return None
    amount, unit = match.groups()
    return int(amount) * minutes_per_unit[unit] * 60


class RemindersFeature:
    def __init__(self, client: discord.Client, tree: app_commands.CommandTree):
        self.client = client
        self.tree = tree
        self._register_commands()

    async def start_tasks(self) -> None:
        if not self._check.is_running():
            self._check.start()

    def _register_commands(self) -> None:
        @self.tree.command(name="remind", description="Set a reminder")
        @app_commands.describe(when="Time (e.g. 30m, 1h)", who="User to remind", what="The message")
        async def remind(
            interaction: discord.Interaction, when: str, who: discord.Member, what: str
        ):
            logger.info(
                f"Command /remind called by {interaction.user} targeting {who.display_name}"
            )
            seconds = _parse_time(when)
            if seconds is None:
                await interaction.response.send_message(
                    "Invalid time format! Use 1m, 1h, or 1d.", ephemeral=True
                )
                return

            remind_at = time.time() + seconds
            db.add_reminder(who.id, interaction.channel_id, remind_at, what)
            await interaction.response.send_message(
                f"Got it! I'll remind {who.display_name} about '{what}' in {when}."
            )

    @tasks.loop(seconds=10)
    async def _check(self):
        try:
            due = db.get_due_reminders()
            for rem_id, user_id, channel_id, message in due:
                try:
                    channel = self.client.get_channel(channel_id)
                    if channel:
                        await channel.send(
                            f"🔔 <@{user_id}>, here is your reminder: **{message}**",
                            suppress_embeds=True,
                        )
                        logger.info(f"Delivered reminder {rem_id} to user {user_id}")
                    else:
                        logger.warning(f"Reminder {rem_id}: Channel {channel_id} inaccessible")
                except Exception as e:
                    logger.error(f"Failed to send reminder {rem_id}: {e}")
                finally:
                    # CRITICAL: always delete the reminder so we don't spam errors infinitely.
                    db.delete_reminder(rem_id)
        except Exception:
            logger.exception("Critical error inside check_reminders loop")
