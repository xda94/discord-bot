from __future__ import annotations

import logging
import random
import re

import discord
from discord import app_commands

import db
from features.response_gate import ResponseGate
from features.sponsors import SponsorsFeature

logger = logging.getLogger("discord_bot")


def _pick_response(options: list[str], last_one: str | None) -> str:
    """Pick a response, preferring anything other than `last_one` when an
    alternative exists.

    Falls back to picking from `options` itself when every entry equals
    `last_one` (e.g. the user added the same response twice for one
    keyword). The previous `while new_response == last_one` loop would
    spin forever in that case — see `tests/test_keywords.py`."""
    alternatives = [r for r in options if r != last_one]
    return random.choice(alternatives or options)


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
        # Per (guild_id, keyword) so the same keyword in two servers doesn't
        # share the "don't repeat last response" state.
        self._last_used: dict[str, str] = {}
        self._register_commands()

    async def handle_message(self, message: discord.Message) -> bool:
        """Return True if a keyword reply was sent (so the caller can stop
        dispatching to other features)."""
        if not self.gate.can_respond():
            return False

        if not message.guild:
            return False

        guild_id = message.guild.id
        content = message.content.lower()
        try:
            responses_data = db.get_all_responses(guild_id)
        except Exception:
            logger.exception("Error fetching responses for keyword match")
            return False

        for keyword, options in responses_data.items():
            if not options:
                continue
            if not re.search(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)", content):
                continue

            last_key = f"{guild_id}:{keyword}"
            new_response = _pick_response(options, self._last_used.get(last_key))
            self._last_used[last_key] = new_response

            suffix = self.sponsors.maybe_get_sponsor_suffix()
            if suffix:
                new_response += suffix

            try:
                await message.reply(new_response, mention_author=False, suppress_embeds=False)
            except Exception:
                logger.exception(f"Failed to send keyword reply for '{keyword}'")
                return False

            self.gate.mark_responded()
            db.log_keyword_usage(keyword, message.author.id, guild_id)
            logger.info(
                f"Triggered response for '{keyword}' in guild {guild_id} #{message.channel}"
            )
            return True

        return False

    def _register_commands(self) -> None:
        @self.tree.command(name="keyword_add", description="Add a new keyword and response")
        @app_commands.describe(keyword="The keyword", response="The response")
        async def keyword_add(interaction: discord.Interaction, keyword: str, response: str):
            logger.info(f"Command /keyword_add called by {interaction.user} for '{keyword}'")
            if not interaction.guild:
                await interaction.response.send_message(
                    "This command must be run inside a server, not in DMs.",
                    ephemeral=True,
                )
                return
            db.add_response(keyword, response, interaction.guild.id)
            await interaction.response.send_message(
                f"Added keyword **{keyword}** for this server."
            )

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
