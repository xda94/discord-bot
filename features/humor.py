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
    purchase_voices, safe_text, text,
)
from llm.humor import generate_humor
from wishlist.alerts import ALERT_LOW_REALERT_DROP_PCT, ALERT_MIN_DATA_POINTS


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
    def money(value):
        return f"{_price_label(value, language)} {currency}" if value is not None else text("unknown", language)

    if stock == "out_of_stock":
        decision = "unavailable"
    elif price is None or not item[6] or len(prices) < ALERT_MIN_DATA_POINTS:
        decision = "insufficient"
    elif stale or not _positive(checked) or stock == "unknown":
        decision = "refresh"
    elif max(maximum, price) < min(minimum, price) * (1 + ALERT_LOW_REALERT_DROP_PCT):
        decision = "neutral"
    elif price > median:
        decision = "wait"
    elif price < median or price <= minimum:
        decision = "buy"
    else:
        decision = "neutral"
    lines = [
        f"**{safe_text(item[5] or '—', 180)}**",
        text("purchase_current", language, price=money(price)),
        text("purchase_stats", language, minimum=money(minimum), maximum=money(maximum), median=money(median)),
    ]
    if price is not None and median is not None:
        median_delta = (price / median - 1) * 100
        minimum_delta = (price / minimum - 1) * 100
        if math.isfinite(median_delta) and math.isfinite(minimum_delta):
            lines.append(text("purchase_comparison", language, median_delta=f"{median_delta:+.1f}%", minimum_delta=f"{minimum_delta:+.1f}%"))
    lines.extend([
        text("purchase_verdict", language, verdict=text("purchase_" + decision, language), reason=text("purchase_reason_" + decision, language, median=money(median))),
        text("purchase_observations", language, count=len(prices), checked=_date_label(checked, language)),
    ])
    facts = {"title": str(item[5] or "")[:180], "saved_price": price, "native_currency": str(item[6] or "unknown")[:15], "saved_stock": stock, "stale": stale, "last_check_known": _positive(checked), "history_samples": len(prices), "recorded_minimum": minimum, "recorded_maximum": maximum, "recorded_median": median, "decision": decision, "median_display": money(median), "historical_currency_assumption": "same native units as this tracked item; no currency conversion"}
    return "\n".join(lines), facts


def purchase_fallback(language, facts=None):
    return purchase_voices(language, facts or {})


def horoscope(guild_id, user_id, day, theme, language):
    theme = theme if theme in HOROSCOPE_THEMES else "general"
    digest = hashlib.sha256(f"{guild_id}:{user_id}:{day.isoformat()}:{theme}".encode()).digest()
    sign = HOROSCOPE_SIGNS[int.from_bytes(digest[:8], "big") % len(HOROSCOPE_SIGNS)]
    forecasts = HOROSCOPE_FORECASTS[theme]
    forecast = forecasts[int.from_bytes(digest[8:16], "big") % len(forecasts)]
    return text("horoscope", language, day=day.isoformat(), theme=HOROSCOPE_THEMES[theme][language == "ro"], sign=sign[language == "ro"], forecast=forecast[language == "ro"])


async def _private_reply(interaction, content, mention_requester=False):
    prefix = f"<@{interaction.user.id}>\n" if mention_requester else ""
    mentions = discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(id=interaction.user.id)], replied_user=False) if mention_requester else discord.AllowedMentions.none()
    await interaction.followup.send(prefix + content[:2000 - len(prefix)], ephemeral=True, allowed_mentions=mentions, suppress_embeds=True)


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
        result = await generate_humor("excuse", language, {"reason": reason.strip(), "style": style}, fallback)
        request = text("excuse_request", language, reason=safe_text(reason.strip(), 1500), style=EXCUSE_STYLES[style][language == "ro"])
        await _private_reply(interaction, request + "\n\n" + text("excuse_result", language) + "\n" + result, mention_requester=True)

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
        fallback = purchase_fallback(language, facts)
        result = await generate_humor("worth-it", language, facts, fallback)
        if len(summary) + len(result) + 2 + len(f"<@{interaction.user.id}>\n") > 2000:
            result = fallback
        await _private_reply(interaction, summary + "\n\n" + result, mention_requester=True)

    async def forecast(self, interaction, theme=None):
        await interaction.response.defer(ephemeral=interaction.guild_id is None)
        language = await interaction_language(interaction)
        if interaction.guild_id is None:
            await interaction.followup.send(text("guild_only", language), ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
            return
        await interaction.followup.send(horoscope(interaction.guild_id, interaction.user.id, local_day(), theme or "general", language), allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)

    def _register_commands(self):
        @self.tree.command(name="excuse", description="Generate a humorous excuse visible only to you")
        @app_commands.describe(reason="Reason, maximum 500 characters", style="Excuse style")
        @app_commands.choices(style=[app_commands.Choice(name=en, value=key) for key, (en, ro) in EXCUSE_STYLES.items()])
        async def excuse(interaction: discord.Interaction, reason: str, style: app_commands.Choice[str]):
            await self.excuse(interaction, reason, style.value)

        @self.tree.command(name="worth-it", description="Privately compare saved prices and get a buy-or-wait verdict")
        @app_commands.describe(url="URL in your saved wishlist")
        async def worth_it(interaction: discord.Interaction, url: str):
            await self.purchase(interaction, url)

        @self.tree.command(name="horoscope", description="Show your daily humorous server horoscope")
        @app_commands.guild_only()
        @app_commands.describe(theme="Horoscope theme")
        @app_commands.choices(theme=[app_commands.Choice(name=en, value=key) for key, (en, ro) in HOROSCOPE_THEMES.items()])
        async def horoscope(interaction: discord.Interaction, theme: Optional[app_commands.Choice[str]] = None):
            await self.forecast(interaction, theme.value if theme else None)
