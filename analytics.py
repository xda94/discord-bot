from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands

import db
from mention_utils import is_bot_mentioned

logger = logging.getLogger("discord_bot")


def guild_id_from(value) -> int | None:
    guild = getattr(value, "guild", None)
    guild_id = getattr(value, "guild_id", None)
    return getattr(guild, "id", None) if guild is not None else guild_id


async def record(
    category: str,
    activity: str,
    *,
    guild_id: int | None = None,
    scope_type: str | None = None,
) -> None:
    """Record analytics without blocking or breaking the Discord operation."""
    try:
        await asyncio.to_thread(
            db.record_analytics_activity,
            category,
            activity,
            guild_id=guild_id,
            scope_type=scope_type,
        )
    except Exception as exc:
        logger.warning(
            "Could not record analytics category=%s activity=%s error=%s",
            category,
            activity,
            type(exc).__name__,
        )


async def record_for(category: str, activity: str, value) -> None:
    guild_id = guild_id_from(value)
    await record(
        category,
        activity,
        guild_id=guild_id,
        scope_type="guild" if guild_id is not None else "dm",
    )


async def refresh_command_catalog(tree: app_commands.CommandTree) -> None:
    names = [command.qualified_name for command in tree.walk_commands()]
    try:
        await asyncio.to_thread(db.refresh_analytics_command_catalog, names)
    except Exception:
        logger.exception("Could not refresh analytics command catalog")


async def record_message_mention(message, bot_id: int) -> bool:
    """Record one mention event per incoming human message."""
    if getattr(getattr(message, "author", None), "bot", False):
        return False
    if not is_bot_mentioned(message, bot_id):
        return False
    await record_for("mention", "bot-mention", message)
    return True


class AnalyticsCommandTree(app_commands.CommandTree):
    """Count slash-command demand before callback checks and validation."""

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if interaction.type is not discord.InteractionType.autocomplete:
            data = interaction.data or {}
            name = data.get("name")
            if isinstance(name, str) and name:
                await record_for("command", name, interaction)
        return True

    async def on_error(self, interaction: discord.Interaction, error, /) -> None:
        data = interaction.data or {}
        name = data.get("name")
        await record_for(
            "failure",
            f"command/{name}" if isinstance(name, str) and name else "command/unknown",
            interaction,
        )
        await super().on_error(interaction, error)
