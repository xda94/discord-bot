from __future__ import annotations

import os
import re

import discord

_MENTION_RE = re.compile(r"<@!?(\d+)>")


def get_bot_id() -> int:
    raw = os.getenv("BOT_ID", "").strip()
    if not raw:
        raise ValueError("BOT_ID is not set")
    return int(raw)


def is_bot_mentioned(message: discord.Message, bot_id: int) -> bool:
    return any(user.id == bot_id for user in message.mentions)


def extract_mention_text(message: discord.Message, bot_id: int) -> str | None:
    """Return the text after the bot mention, or '' if only the ping.

    The bot's own mention is removed, and every other user/role/channel mention
    is rendered as a readable name so no raw `<@id>` reaches the LLM. Returns
    None if the bot was not mentioned."""
    if not is_bot_mentioned(message, bot_id):
        return None
    text = re.sub(rf"<@!?{bot_id}>\s*", "", message.content).strip()
    for user in message.mentions:
        if user.id == bot_id:
            continue
        text = text.replace(f"<@{user.id}>", f"@{user.display_name}")
        text = text.replace(f"<@!{user.id}>", f"@{user.display_name}")
    for role in message.role_mentions:
        text = text.replace(f"<@&{role.id}>", f"@{role.name}")
    for channel in message.channel_mentions:
        text = text.replace(f"<#{channel.id}>", f"#{channel.name}")
    return text
