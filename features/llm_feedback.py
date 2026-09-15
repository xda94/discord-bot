from __future__ import annotations

import logging

import discord
from discord import app_commands

import db

logger = logging.getLogger("discord_bot")

THUMBS_UP = "👍"
THUMBS_DOWN = "👎"
RATINGS = {THUMBS_UP: 1, THUMBS_DOWN: -1}
MIN_RATINGS_FOR_COMPARISON = 10


class LLMFeedbackFeature:
    """Own compact, requester-only ratings for mention-generated replies.

    The database stores reply metadata and the final rating, never the prompt,
    response body, channel, or conversation history. Reactions are preferred
    over a command because they are attached to the exact answer being rated.
    """

    def __init__(
        self,
        client: discord.Client,
        tree: app_commands.CommandTree,
        bot_id: int,
    ):
        self.client = client
        self.tree = tree
        self.bot_id = bot_id
        self._register_commands()

    async def register_reply(
        self,
        message: discord.Message,
        *,
        requester_user_id: int,
        category: str,
        model: str,
        prompt_version: str,
    ) -> None:
        """Persist a rateable reply and add its two feedback affordances."""
        message_id = getattr(message, "id", None)
        if message_id is None:
            logger.warning("Cannot register LLM feedback for a reply without an ID")
            return

        guild = getattr(message, "guild", None)
        guild_id = getattr(guild, "id", None)
        if not db.track_llm_response(
            message_id,
            requester_user_id,
            category,
            guild_id=guild_id,
            model=model,
            prompt_version=prompt_version,
        ):
            return

        for emoji in (THUMBS_UP, THUMBS_DOWN):
            try:
                await message.add_reaction(emoji)
            except discord.Forbidden:
                logger.warning(
                    "Cannot add LLM feedback reactions to message %s; "
                    "check Add Reactions permission.",
                    message_id,
                )
                return
            except discord.HTTPException:
                logger.warning("Failed to add LLM feedback reaction to message %s", message_id)
                return

    async def handle_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Accept only the original requester's 👍/👎 on registered replies."""
        if payload.user_id == self.bot_id:
            return
        rating = RATINGS.get(str(payload.emoji))
        if rating is None:
            return
        if db.set_llm_response_rating(payload.message_id, payload.user_id, rating):
            logger.info(
                "LLM feedback recorded: message=%s rating=%s",
                payload.message_id,
                rating,
            )

    def _register_commands(self) -> None:
        @self.tree.command(
            name="llm-feedback-summary",
            description="Show reviewed LLM feedback for this server",
        )
        async def llm_feedback_summary(interaction: discord.Interaction):
            if interaction.guild is None:
                await interaction.response.send_message(
                    "This report is available only inside a server.", ephemeral=True
                )
                return
            permissions = getattr(interaction.user, "guild_permissions", None)
            if permissions is None or not permissions.manage_guild:
                await interaction.response.send_message(
                    "You need the Manage Server permission to view this report.",
                    ephemeral=True,
                )
                return

            rows = db.get_llm_feedback_summary(interaction.guild.id)
            if not rows:
                await interaction.response.send_message(
                    "No rated LLM replies are available for this server yet.",
                    ephemeral=True,
                )
                return

            lines = [
                "**LLM feedback summary**",
                (
                    f"Use combinations with at least "
                    f"{MIN_RATINGS_FOR_COMPARISON} ratings for comparison."
                ),
                "",
            ]
            for category, model, prompt_version, total, positive, negative in rows:
                approval = positive / total * 100 if total else 0
                sample = (
                    "ready to compare"
                    if total >= MIN_RATINGS_FOR_COMPARISON
                    else f"needs {MIN_RATINGS_FOR_COMPARISON - total} more ratings"
                )
                lines.append(
                    f"• {category} | {model} | {prompt_version}: "
                    f"{positive} 👍 / {negative} 👎 "
                    f"({approval:.0f}% approval; {sample})"
                )
            await interaction.response.send_message(
                "\n".join(lines), ephemeral=True
            )
