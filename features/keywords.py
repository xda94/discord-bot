from __future__ import annotations

import logging
import asyncio
import random
import re

import discord
from discord import app_commands

import db
from analytics import record
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
    """Keyword-triggered auto-responses plus the /keyword-add and /top-keywords
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
        self._matchers: dict[int, tuple[dict, re.Pattern | None]] = {}
        self._register_commands()

    def _matcher_for(self, guild_id: int, responses_data: dict) -> re.Pattern | None:
        cached = self._matchers.get(guild_id)
        if cached is not None and cached[0] is responses_data:
            return cached[1]
        keywords = sorted(
            (keyword for keyword, options in responses_data.items() if options),
            key=len,
            reverse=True,
        )
        pattern = None
        if keywords:
            pattern = re.compile(
                r"(?<!\w)(?:" + "|".join(re.escape(k) for k in keywords) + r")(?!\w)"
            )
        self._matchers[guild_id] = (responses_data, pattern)
        return pattern

    async def handle_message(self, message: discord.Message) -> bool:
        """Return True if a keyword reply was sent (so the caller can stop
        dispatching to other features)."""
        if not message.guild:
            return False

        guild_id = message.guild.id
        if not self.gate.can_respond(guild_id):
            return False

        content = message.content.lower()
        try:
            responses_data = db.get_cached_responses(guild_id)
            if responses_data is None:
                responses_data = await asyncio.to_thread(db.get_all_responses, guild_id)
        except Exception:
            logger.exception("Error fetching responses for keyword match")
            return False

        matcher = self._matcher_for(guild_id, responses_data)
        if matcher is None or not matcher.search(content):
            return False

        for keyword, options in responses_data.items():
            if not options:
                continue
            if not re.search(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)", content):
                continue

            last_key = f"{guild_id}:{keyword}"
            new_response = _pick_response(options, self._last_used.get(last_key))
            self._last_used[last_key] = new_response

            suffix = await asyncio.to_thread(self.sponsors.maybe_get_sponsor_suffix)
            if suffix:
                new_response += suffix

            try:
                await message.reply(new_response, mention_author=False, suppress_embeds=False)
            except Exception:
                logger.exception("Failed to send keyword reply for '%s'", keyword)
                await record("failure", "keyword-reply", guild_id=guild_id)
                return False

            self.gate.mark_responded(guild_id)
            await asyncio.to_thread(db.log_keyword_usage,keyword, message.author.id, guild_id)
            await record("automatic", "keyword-reply", guild_id=guild_id)
            if suffix:
                await record("automatic", "sponsor-tagged-reply", guild_id=guild_id)
            logger.info(
                "Triggered response for '%s' in guild %s #%s",
                keyword,
                guild_id,
                message.channel,
            )
            return True

        return False

    def _register_commands(self) -> None:
        @self.tree.command(name="keyword-add", description="Add a new keyword and response")
        @app_commands.describe(keyword="The keyword", response="The response")
        async def keyword_add(interaction: discord.Interaction, keyword: str, response: str):
            logger.info("Command /keyword-add called by %s for '%s'", interaction.user, keyword)
            if not interaction.guild:
                await interaction.response.send_message(
                    "This command must be run inside a server, not in DMs.",
                    ephemeral=True,
                )
                return
            if not await asyncio.to_thread(db.add_response,keyword, response, interaction.guild.id):
                await interaction.response.send_message(
                    "Failed to save this keyword. Please try again.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                f"Added keyword **{keyword}** for this server."
            )

        @self.tree.command(name="top-keywords", description="Show the most used keywords")
        @app_commands.describe(user="Optional: see a specific user's top keywords")
        async def topkeywords(interaction: discord.Interaction, user: discord.Member | None = None):
            logger.info(
                "Command /top-keywords called by %s%s",
                interaction.user,
                f" for {user.display_name}" if user else "",
            )
            if not interaction.guild:
                await interaction.response.send_message(
                    "This command can only be used in a server.", ephemeral=True
                )
                return

            guild_id = interaction.guild.id
            if user:
                rows = await asyncio.to_thread(db.get_top_keywords_by_user,guild_id, user.id)
                title = f"Top keywords for {user.display_name}"
            else:
                rows = await asyncio.to_thread(db.get_top_keywords,guild_id)
                title = "Top keywords in this server"

            if not rows:
                await interaction.response.send_message("No keyword usage data yet.", ephemeral=True)
                return

            lines = [f"**{title}**"]
            for i, (keyword, count) in enumerate(rows, 1):
                display = keyword if keyword.startswith("<@") else f"`{keyword}`"
                lines.append(f"{i}. {display} — {count} use{'s' if count != 1 else ''}")
            await interaction.response.send_message("\n".join(lines))
