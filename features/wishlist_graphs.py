"""Saved wishlist graph data and requester-owned Discord navigation."""

from __future__ import annotations

import asyncio
import io
import logging
import math
import time
from datetime import datetime, timezone
from functools import partial

import discord

import db
from analytics import record_for
from chart_renderer import render_multi_price_history_png, render_price_history_png

logger = logging.getLogger("discord_bot")
GRAPH_MAX_DAYS = 180


def validate_days(days: int) -> int:
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= GRAPH_MAX_DAYS:
        raise ValueError(f"Days must be a whole number from 1 to {GRAPH_MAX_DAYS}.")
    return days


def build_graph(user_id, url, currency, days, percentage, converter, effective_currency):
    """Build from owned, saved observations only; safe to run in a worker thread."""
    validate_days(days)
    now = time.time()
    cutoff = now - days * 86400
    if url is not None:
        item = db.get_scraped_item(user_id, url)
        if item is None:
            return "That URL is not in your tracking list.", None
        items = [(item[2], item[3], item[4], item[5], item[6])]
    else:
        item = None
        items = db.get_user_scraped_items(user_id)
    if not items:
        return "You are not tracking any items.", None

    series = []
    skipped = 0
    for item_url, _, _, title, stored_currency in items:
        source = effective_currency(stored_currency, item_url)
        points = []
        for price, ts, _ in db.get_price_history(user_id, item_url):
            if price is None or not cutoff <= ts <= now:
                continue
            value = float(price) if percentage else converter.to_currency(price, source, currency)
            if value is not None and math.isfinite(value):
                points.append((datetime.fromtimestamp(ts, timezone.utc), value))
        if not points or (percentage and points[0][1] <= 0):
            skipped += 1
            continue
        series.append((title or item_url, points))

    mode = "percentage change" if percentage else currency
    content = f"Last **{days} days** · {mode} · dates in UTC."
    if not series:
        return (
            content + " No usable observations in this period. Try a longer range. "
            "Currency views need a known currency/rate; percentage views need a positive starting price.",
            None,
        )
    if skipped:
        content += (
            f" Skipped {skipped} item(s): no observations, unavailable currency conversion, "
            "or nonpositive starting price."
        )
    if item is not None:
        target = (
            converter.to_currency(item[9], item[10], currency)
            if item[9] is not None and item[10] else None
        )
        label, points = series[0]
        png = render_price_history_png(
            [p[0] for p in points], [p[1] for p in points], label, currency, target
        )
        filename = "price_history.png"
    else:
        png = render_multi_price_history_png(series, currency, percentage=percentage)
        filename = "price_history_all.png"
        content += f" Showing {len(series)} product(s)."
        if percentage:
            content += " Each starts at 0% at its first observation in this period."
    return content, discord.File(io.BytesIO(png), filename=filename)


class CustomDaysModal(discord.ui.Modal):
    def __init__(self, graph_view):
        super().__init__(title="Custom graph period")
        self.graph_view = graph_view
        self.days_input = discord.ui.TextInput(
            label=f"Number of days (1–{GRAPH_MAX_DAYS})",
            default=str(graph_view.days), min_length=1, max_length=3,
        )
        self.add_item(self.days_input)

    async def on_submit(self, interaction):
        try:
            days = validate_days(int(self.days_input.value.strip()))
        except ValueError:
            await interaction.response.send_message(
                f"Enter a whole number from 1 to {GRAPH_MAX_DAYS}.", ephemeral=True
            )
            return
        await self.graph_view.update_graph(
            interaction, days=days, analytics_activity="wishlist-graph-custom-period"
        )


class WishlistGraphView(discord.ui.View):
    def __init__(self, user_id, build, *, days=GRAPH_MAX_DAYS, percentage=False, allow_percentage=False):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.build = build
        self.days = validate_days(days)
        self.percentage = percentage
        self._lock = asyncio.Lock()
        for period in (7, 30, 90):
            button = discord.ui.Button(label=f"{period} days", custom_id=f"graph-days-{period}")

            async def select_period(interaction, selected=period):
                await self.update_graph(
                    interaction, days=selected, analytics_activity="wishlist-graph-period-button"
                )

            button.callback = select_period
            self.add_item(button)
        custom = discord.ui.Button(label="Custom days", custom_id="graph-custom-days")

        async def show_custom(interaction):
            await interaction.response.send_modal(CustomDaysModal(self))

        custom.callback = show_custom
        self.add_item(custom)
        if allow_percentage:
            toggle = discord.ui.Button(label="", custom_id="graph-toggle-percentage", row=1)

            async def toggle_percentage(interaction):
                await self.update_graph(
                    interaction, toggle=True, analytics_activity="wishlist-graph-toggle-button"
                )

            toggle.callback = toggle_percentage
            self.add_item(toggle)
        self._update_buttons()

    def _update_buttons(self):
        for button in self.children:
            if button.custom_id.startswith("graph-days-"):
                selected = int(button.custom_id.rsplit("-", 1)[1]) == self.days
                button.style = discord.ButtonStyle.primary if selected else discord.ButtonStyle.secondary
            elif button.custom_id == "graph-toggle-percentage":
                button.label = "Show prices" if self.percentage else "Compare % change"

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Run your own wishlist graph command to use these controls.", ephemeral=True)
            return False
        if self.is_finished():
            await interaction.response.send_message("These controls expired. Run the graph command again.", ephemeral=True)
            return False
        return True

    async def update_graph(
        self, interaction, *, days=None, toggle=False, analytics_activity="wishlist-graph-control"
    ):
        if not await self.interaction_check(interaction):
            return
        await record_for("control", analytics_activity, interaction)
        if self._lock.locked():
            await interaction.response.send_message("Your graph is still rendering. Please wait.", ephemeral=True)
            return
        async with self._lock:
            await interaction.response.defer()
            new_days = self.days if days is None else validate_days(days)
            new_percentage = not self.percentage if toggle else self.percentage
            file = None
            try:
                content, file = await asyncio.to_thread(self.build, new_days, new_percentage)
                old_days, old_percentage = self.days, self.percentage
                self.days, self.percentage = new_days, new_percentage
                self._update_buttons()
                try:
                    await interaction.edit_original_response(
                        content=content, attachments=[file] if file else [], view=self,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except Exception:
                    self.days, self.percentage = old_days, old_percentage
                    self._update_buttons()
                    raise
            except Exception:
                logger.exception("Could not update wishlist graph for user %s", self.user_id)
                await record_for("failure", analytics_activity, interaction)
                await interaction.followup.send("Could not render the graph. Please try again.", ephemeral=True)
            finally:
                if file:
                    file.close()


async def send_graph(interaction, *, url, currency, days, percentage, converter, effective_currency):
    try:
        validate_days(days)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    build = partial(
        build_graph, interaction.user.id, url, currency,
        converter=converter, effective_currency=effective_currency,
    )
    file = None
    try:
        content, file = await asyncio.to_thread(build, days, percentage)
        view = WishlistGraphView(
            interaction.user.id, build, days=days, percentage=percentage,
            allow_percentage=url is None,
        )
        kwargs = {"content": content, "view": view, "ephemeral": True,
                  "allowed_mentions": discord.AllowedMentions.none()}
        if file:
            kwargs["file"] = file
        await interaction.followup.send(**kwargs)
    except Exception:
        logger.exception("Could not build wishlist graph for user %s", interaction.user.id)
        await interaction.followup.send("Could not render the graph. Please try again.", ephemeral=True)
    finally:
        if file:
            file.close()
