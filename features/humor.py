import asyncio
import hashlib
import math
import sqlite3
import time
from datetime import datetime
from typing import Optional

import discord
from discord import app_commands

import db
from features.humor_common import (
    EXCUSE_FALLBACKS, EXCUSE_STYLES, HOROSCOPE_FORECASTS,
    HOROSCOPE_SIGNS, HOROSCOPE_THEMES, interaction_language, local_day,
    safe_text, text,
)
from llm.humor import generate_humor


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _date_label(value, language):
    if not _positive(value):
        return text("unknown", language)
    try:
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return text("unknown", language)


def _price_label(value, language):
    if value is None:
        return text("unknown", language)
    rendered = f"{value:.2f}"
    return rendered if len(rendered) <= 40 else f"{value:.6g}"


def purchase_summary(item, history, language, now=None):
    now = time.time() if now is None else now
    prices = [row[0] for row in history if isinstance(row, (list, tuple)) and len(row) >= 2 and _positive(row[0])]
    timestamps = [row[1] for row in history if isinstance(row, (list, tuple)) and len(row) >= 2 and _positive(row[0]) and _positive(row[1])]
    price = item[3] if _positive(item[3]) else None
    currency = safe_text(item[6], 15) if item[6] else text("unknown", language)
    checked = item[13]
    stale = _positive(checked) and now - checked > 86400
    stock = "unknown" if item[4] is None else "in_stock" if item[4] else "out_of_stock"
    minimum = min(prices) if prices else None
    maximum = max(prices) if prices else None
    ordered = sorted(prices)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else ordered[middle - 1] + (ordered[middle] - ordered[middle - 1]) / 2 if ordered else None
    lines = [
        text("saved_item", language, title=safe_text(item[5] or "—", 180)),
        text("saved_price", language, price=_price_label(price, language), currency=currency),
        text("saved_stock", language, stock=text(stock, language)),
        text("last_check", language, checked=_date_label(checked, language), freshness=text("stale" if stale else "fresh" if _positive(checked) else "unknown", language)),
        text("history_summary", language, count=len(prices), start=_date_label(min(timestamps) if timestamps else None, language), end=_date_label(max(timestamps) if timestamps else None, language)),
        text("history_stats", language, minimum=_price_label(minimum, language), maximum=_price_label(maximum, language), median=_price_label(median, language)),
        text("history_currency", language),
    ]
    if len(prices) < 2:
        lines.append(text("history_sparse", language))
    lines.append(text("saved_only", language))
    facts = {"title": str(item[5] or "")[:180], "saved_price": price, "native_currency": str(item[6] or "unknown")[:15], "saved_stock": stock, "stale": stale, "last_check_known": _positive(checked), "history_samples": len(prices), "recorded_minimum": minimum, "recorded_maximum": maximum, "recorded_median": median, "historical_currency": "not stored; do not compare with current currency"}
    return "\n".join(lines), facts


def purchase_fallback(language):
    return (
        f"**{text('purchase_advocate', language)}:** {text('purchase_fallback_advocate', language)}\n"
        f"**{text('household_accountant', language)}:** {text('purchase_fallback_accountant', language)}"
    )


def horoscope(guild_id, user_id, day, theme, language):
    theme = theme if theme in HOROSCOPE_THEMES else "general"
    digest = hashlib.sha256(f"{guild_id}:{user_id}:{day.isoformat()}:{theme}".encode()).digest()
    sign = HOROSCOPE_SIGNS[int.from_bytes(digest[:8], "big") % len(HOROSCOPE_SIGNS)]
    forecasts = HOROSCOPE_FORECASTS[theme]
    forecast = forecasts[int.from_bytes(digest[8:16], "big") % len(forecasts)]
    return text("horoscope", language, day=day.isoformat(), theme=HOROSCOPE_THEMES[theme][language == "ro"], sign=sign[language == "ro"], forecast=forecast[language == "ro"])


async def _private_reply(interaction, content, language):
    try:
        await interaction.user.send(content[:2000], allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
    except (discord.HTTPException, OSError):
        await interaction.followup.send(text("dm_failed", language), ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
        return
    await interaction.followup.send(text("dm_sent", language), ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)


class HumorFeature:
    def __init__(self, client, tree):
        self.client = client
        self.tree = tree
        self._register_commands()

    async def excuse(self, interaction, reason, style):
        await interaction.response.defer(ephemeral=True)
        language = await interaction_language(interaction)
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500 or style not in EXCUSE_STYLES:
            await interaction.followup.send(text("excuse_invalid", language), ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
            return
        fallback = EXCUSE_FALLBACKS[style][language == "ro"].format(reason=safe_text(reason.strip(), 1000))
        result = await generate_humor("scuza", language, {"reason": reason.strip(), "style": style}, fallback)
        await _private_reply(interaction, result, language)

    async def purchase(self, interaction, url):
        await interaction.response.defer(ephemeral=True)
        language = await interaction_language(interaction)
        if not isinstance(url, str) or not 1 <= len(url.strip()) <= 1500:
            await interaction.followup.send(text("url_invalid", language), ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
            return
        try:
            item = await asyncio.to_thread(db.get_scraped_item, interaction.user.id, url.strip())
            if item is None:
                await interaction.followup.send(text("wishlist_missing", language), ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
                return
            history = await asyncio.to_thread(db.get_price_history, interaction.user.id, url.strip())
            summary, facts = purchase_summary(item, history, language)
        except (sqlite3.Error, OSError, ValueError, TypeError, IndexError):
            await interaction.followup.send(text("failed", language), ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
            return
        fallback = purchase_fallback(language)
        result = await generate_humor("merita", language, facts, fallback)
        if len(summary) + len(result) + 2 > 2000:
            result = fallback
        await _private_reply(interaction, summary + "\n\n" + result, language)

    async def forecast(self, interaction, theme=None):
        await interaction.response.defer(ephemeral=interaction.guild_id is None)
        language = await interaction_language(interaction)
        if interaction.guild_id is None:
            await interaction.followup.send(text("guild_only", language), ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
            return
        await interaction.followup.send(horoscope(interaction.guild_id, interaction.user.id, local_day(), theme or "general", language), allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)

    def _register_commands(self):
        @self.tree.command(name="scuza", description="O scuză amuzantă în mesaj privat / A humorous excuse by DM")
        @app_commands.describe(motiv="Motivul / Reason (maximum 500 characters)", stil="Stilul scuzei / Excuse style")
        @app_commands.choices(stil=[app_commands.Choice(name=f"{ro} / {en}", value=key) for key, (en, ro) in EXCUSE_STYLES.items()])
        async def scuza(interaction: discord.Interaction, motiv: str, stil: app_commands.Choice[str]):
            await self.excuse(interaction, motiv, stil.value)

        @self.tree.command(name="merita", description="Dezbatere privată din lista salvată / Private saved-wishlist debate")
        @app_commands.describe(url="Linkul din lista ta salvată / URL in your saved wishlist")
        async def merita(interaction: discord.Interaction, url: str):
            await self.purchase(interaction, url)

        @self.tree.command(name="horoscop", description="Horoscop amuzant zilnic pe server / Daily server humor horoscope")
        @app_commands.guild_only()
        @app_commands.describe(tema="Tema / Theme")
        @app_commands.choices(tema=[app_commands.Choice(name=f"{ro} / {en}", value=key) for key, (en, ro) in HOROSCOPE_THEMES.items()])
        async def horoscop(interaction: discord.Interaction, tema: Optional[app_commands.Choice[str]] = None):
            await self.forecast(interaction, tema.value if tema else None)
