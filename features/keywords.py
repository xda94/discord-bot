import logging
import random
import re

import discord
from discord import app_commands

import db
from features.response_gate import ResponseGate
from features.sponsors import SponsorsFeature

logger = logging.getLogger("discord_bot")


class KeywordsFeature:
    """Keyword-triggered auto-responses plus the /keyword_add and /topkeywords
    commands."""

    def __init__(
        self,
        client: discord.Client,
        tree: app_commands.CommandTree,
        gate: ResponseGate,
        sponsors: SponsorsFeature,
    ):
        self.client = client
        self.tree = tree
        self.gate = gate
        self.sponsors = sponsors
        self._last_used: dict[str, str] = {}
        self._register_commands()

    async def handle_message(self, message: discord.Message) -> bool:
        """Return True if a keyword reply was sent (so the caller can stop
        dispatching to other features)."""
        if not self.gate.can_respond():
            return False

        content = message.content.lower()
        try:
            responses_data = db.get_all_responses()
        except Exception:
            logger.exception("Error fetching responses for keyword match")
            return False

        for keyword, options in responses_data.items():
            if not options:
                continue
            if not re.search(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)", content):
                continue

            new_response = random.choice(options)
            if len(options) > 1:
                last_one = self._last_used.get(keyword)
                while new_response == last_one:
                    new_response = random.choice(options)
            self._last_used[keyword] = new_response

            suffix = self.sponsors.maybe_get_sponsor_suffix()
            if suffix:
                new_response += suffix

            try:
                await message.reply(new_response, mention_author=False, suppress_embeds=True)
            except Exception:
                logger.exception(f"Failed to send keyword reply for '{keyword}'")
                return False

            self.gate.mark_responded()
            if message.guild:
                db.log_keyword_usage(keyword, message.author.id, message.guild.id)
            logger.info(f"Triggered response for '{keyword}' in #{message.channel}")
            return True

        return False

    def _register_commands(self) -> None:
        @self.tree.command(name="keyword_add", description="Add a new keyword and response")
        @app_commands.describe(keyword="The keyword", response="The response")
        async def keyword_add(interaction: discord.Interaction, keyword: str, response: str):
            logger.info(f"Command /keyword_add called by {interaction.user} for '{keyword}'")
            db.add_response(keyword, response)
            await interaction.response.send_message(f"Added keyword: **{keyword}**")

        @self.tree.command(name="topkeywords", description="Show the most used keywords")
        @app_commands.describe(user="Optional: see a specific user's top keywords")
        async def topkeywords(interaction: discord.Interaction, user: discord.Member | None = None):
            logger.info(
                f"Command /topkeywords called by {interaction.user}"
                + (f" for {user.display_name}" if user else "")
            )
            if not interaction.guild:
                await interaction.response.send_message(
                    "This command can only be used in a server.", ephemeral=True
                )
                return

            guild_id = interaction.guild.id
            if user:
                rows = db.get_top_keywords_by_user(guild_id, user.id)
                title = f"Top keywords for {user.display_name}"
            else:
                rows = db.get_top_keywords(guild_id)
                title = "Top keywords in this server"

            if not rows:
                await interaction.response.send_message("No keyword usage data yet.", ephemeral=True)
                return

            lines = [f"**{title}**"]
            for i, (keyword, count) in enumerate(rows, 1):
                display = keyword if keyword.startswith("<@") else f"`{keyword}`"
                lines.append(f"{i}. {display} — {count} use{'s' if count != 1 else ''}")
            await interaction.response.send_message("\n".join(lines))
