from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

import db
from analytics import record
from llm.feedback import build_feedback_report

logger = logging.getLogger("discord_bot")

THUMBS_UP = "👍"
THUMBS_DOWN = "👎"
RATINGS = {THUMBS_UP: 1, THUMBS_DOWN: -1}
MIN_RATINGS_FOR_COMPARISON = 10


def _chunk_feedback_report(text: str) -> list[str]:
    chunks = []
    remaining = text
    while len(remaining) > 1900:
        boundary = remaining.rfind("\n", 0, 1900)
        if boundary <= 0:
            boundary = 1900
        else:
            boundary += 1
        chunks.append(remaining[:boundary])
        remaining = remaining[boundary:]
    if remaining:
        chunks.append(remaining)
    return chunks


class LLMFeedbackFeature:
    """Own compact, requester-only ratings for mention-generated replies.

    The database stores reply metadata and the final rating, never the prompt,
    response body, channel, or conversation history. Only a reaction manually
    added by the original requester is accepted; the bot does not seed feedback
    reactions on its own replies.
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
        """Persist a rateable reply without adding reactions on the bot's behalf."""
        message_id = getattr(message, "id", None)
        if message_id is None:
            logger.warning("Cannot register LLM feedback for a reply without an ID")
            return

        guild = getattr(message, "guild", None)
        guild_id = getattr(guild, "id", None)
        db.track_llm_response(
            message_id,
            requester_user_id,
            category,
            guild_id=guild_id,
            model=model,
            prompt_version=prompt_version,
        )

    async def handle_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Accept only the original requester's 👍/👎 on registered replies."""
        if payload.user_id == self.bot_id:
            return
        rating = RATINGS.get(str(payload.emoji))
        if rating is None:
            return
        if db.set_llm_response_rating(payload.message_id, payload.user_id, rating):
            guild_id = getattr(payload, "guild_id", None)
            await record(
                "feedback",
                "llm-rating",
                guild_id=guild_id,
                scope_type="guild" if guild_id is not None else "dm",
            )
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
                    "This report is available only inside a server.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            permissions = getattr(interaction.user, "guild_permissions", None)
            if permissions is None or not permissions.manage_guild:
                await interaction.response.send_message(
                    "You need the Manage Server permission to view this report.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            await interaction.response.defer(ephemeral=True)
            try:
                rows = await asyncio.to_thread(
                    db.get_llm_feedback_summary,
                    interaction.guild.id,
                    raise_on_error=True,
                )
            except Exception:
                logger.exception("Failed to read LLM feedback summary")
                await interaction.followup.send(
                    "Could not load LLM feedback right now. Please retry this command.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            report = build_feedback_report(rows)
            if not report["groups"]:
                await interaction.followup.send(
                    "No rated LLM replies are available for this server yet.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            lines = [
                "**LLM feedback summary**",
                (
                    f"Review requires {report['required_likes']} likes per configuration; "
                    f"{report['qualified_group_count']} group(s) qualified for review."
                ),
                "No change applied. Administrator approval is required before "
                "any proposed prompt or model change is applied.",
                "",
            ]
            for group in report["groups"]:
                comparison = (
                    "ready to compare"
                    if group["ready_to_compare"]
                    else f"needs {MIN_RATINGS_FOR_COMPARISON - group['ratings']} more ratings for comparison"
                )
                review = (
                    "qualified for review; approval required"
                    if group["qualified_for_review"]
                    else f"needs {group['likes_needed']} more likes for review"
                )
                lines.append(
                    f"• {group['category']} | {group['model']} | {group['prompt_version']}: "
                    f"{group['ratings']} ratings; {group['up']} 👍 / {group['down']} 👎 "
                    f"({group['approval_percent']:.1f}% approval; {comparison}; {review})"
                )
                lines.extend(f"  • {item}" for item in group["recommendations"])
            for chunk in _chunk_feedback_report("\n".join(lines)):
                await interaction.followup.send(
                    chunk,
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
