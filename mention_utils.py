from __future__ import annotations

import os
import re

import discord

_MENTION_RE = re.compile(r"<@!?(\d+)>")


def strip_leading_reply_labels(
    text: str, *, requester_id: int | None, names: tuple[str, ...]
) -> str:
    """Clean addressing before both validation and Discord delivery."""
    cleaned = text.strip()
    if requester_id is not None:
        cleaned = re.sub(rf"^(?:\s*<@!?{requester_id}>\s*)+", "", cleaned)
    usable = sorted(
        {name.strip() for name in names if name and name.strip()},
        key=len,
        reverse=True,
    )
    if not usable:
        return cleaned
    alternatives = "|".join(re.escape(name) for name in usable)
    leading_at_name = re.compile(
        rf"^\s*(?:[*_`~]{{1,3}})?\s*@(?:{alternatives})\b\s*"
        rf"(?:[*_`~]{{1,3}})?\s*",
        flags=re.IGNORECASE,
    )
    label = re.compile(
        rf"^\s*(?:[*_`~]{{1,3}})?\s*"
        rf"(?:@?(?:{alternatives}))(?:\s+@?(?:{alternatives}))*"
        rf"\s*(?:[*_`~]{{1,3}})?\s*[:：\-–—]\s*"
        rf"(?:[*_`~]{{1,3}})?\s*",
        flags=re.IGNORECASE,
    )
    for pattern in (leading_at_name, label):
        while True:
            updated = pattern.sub("", cleaned, count=1).lstrip()
            if updated == cleaned:
                break
            cleaned = updated
    return cleaned or text.strip()


def get_bot_id() -> int:
    raw = os.getenv("BOT_ID", "").strip()
    if not raw:
        raise ValueError("BOT_ID is not set")
    return int(raw)


def is_bot_mentioned(message: discord.Message, bot_id: int) -> bool:
    return any(user.id == bot_id for user in message.mentions)


def resolve_bot_display_name(
    guild: discord.Guild | None, client: discord.Client
) -> str:
    """The bot's name in this context: its server nickname if set, otherwise its
    global username. Empty string if the client isn't ready yet. Resolved live so
    renaming the bot on the server takes effect immediately."""
    if guild is not None and guild.me is not None:
        return guild.me.display_name
    if client.user is not None:
        return client.user.display_name
    return ""


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
