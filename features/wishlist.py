from __future__ import annotations

import asyncio
import io
import logging
import time
from datetime import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import tasks

import db
from analytics import record
from features.wishlist_graphs import GRAPH_MAX_DAYS, send_graph
from wishlist.charts import render_multi_price_history_png, render_price_history_png
from llm.responses import generate_price_change_message

from wishlist.refresh import refresh_item
from wishlist.scraper import (
    FAILURE_BLOCKED,
    FAILURE_UNSUPPORTED,
    PriceScraper,
    ScrapeResult,
    _domain,
    _is_valid_http_url,
)

logger = logging.getLogger("discord_bot")

# Size of the rolling per-item price-history window. At every scrape pass
# the loop trims rows older than this many days, so each tracked item
# always shows roughly the most recent N days of history regardless of how
# long it's been tracked — items are NOT wiped on their 180-day anniversary,
# the window just slides forward one pass at a time.
#
# At a 12 h scrape cadence and 100 tracked items this is ~36 k rows /
# ~2.5 MB at steady state. Backed by
# `idx_price_history_timestamp` so the periodic cleanup stays fast as
# the table grows.
PRICE_HISTORY_RETENTION_DAYS = GRAPH_MAX_DAYS
MANUAL_REFRESH_COOLDOWN_SECONDS = 300


from wishlist.alerts import (
    ALERT_LOW_REALERT_DROP_PCT,
    ALERT_MIN_DATA_POINTS,
    AlertDecision,
    classify_price as _classify_price,
)
from wishlist.currency import (
    CurrencyConverter,
    effective_currency as _effective_currency,
    majority_currency as _majority_currency,
)

CURRENCY_CHOICES = [
    app_commands.Choice(name=currency, value=currency)
    for currency in CurrencyConverter.SUPPORTED_DISPLAY_CURRENCIES
]

# ---------------------------------------------------------------------------
# Feature wiring
# ---------------------------------------------------------------------------


class WishlistFeature:
    """Owns all price-tracking commands, exchange rate refreshes, and the
    periodic price-scrape DM task."""

    def __init__(self, client: discord.Client, tree: app_commands.CommandTree):
        self.client = client
        self.tree = tree
        self.converter = CurrencyConverter()
        self.scraper = PriceScraper()
        self._manual_refresh_at: dict[tuple[int, str], float] = {}
        self._register_commands()

    async def start_tasks(self) -> None:
        if not self._scrape_loop.is_running():
            self._scrape_loop.start()
        if not self._refresh_rates_loop.is_running():
            self._refresh_rates_loop.start()

    def _register_commands(self) -> None:
        feature = self

        @self.tree.command(name="wishlist-item", description="Add a link to track price and stock")
        @app_commands.describe(url="The URL of the item to track")
        async def scrape_item(interaction: discord.Interaction, url: str):
            logger.info(f"Command /wishlist-item called by {interaction.user} for {url}")
            await interaction.response.defer(ephemeral=True)

            # Reject obvious nonsense before we burn a network round-trip.
            if not _is_valid_http_url(url):
                await interaction.followup.send(
                    "❌ That doesn't look like a valid HTTP(S) URL. "
                    "Expected something like `https://example.com/product/123`.",
                    ephemeral=True,
                )
                return

            # `fetch` does a synchronous HTTP round-trip (up to 15s timeout)
            # plus HTML parsing. Run it in a worker thread so the bot's event
            # loop is free to handle other commands/messages while we wait.
            result = await asyncio.to_thread(feature.scraper.fetch, url)

            if result.failure == FAILURE_BLOCKED:
                await interaction.followup.send(
                    f"❌ The domain `{_domain(url)}` is blocking the scraper "
                    f"(TLS/Cloudflare anti-bot protection or the page took too long to respond). "
                    f"The link was **not** added.",
                    ephemeral=True,
                    suppress_embeds=True,
                )
                return

            if result.failure == FAILURE_UNSUPPORTED:
                await interaction.followup.send(
                    "❌ Reached the page, but couldn't find any price/stock data in the "
                    "supported formats (JSON-LD, meta tags, plain text). It's most likely "
                    "a JavaScript-rendered page. The link was **not** added.",
                    ephemeral=True,
                    suppress_embeds=True,
                )
                return

            item_id = db.add_scraped_item(
                interaction.user.id, url,
                result.title, result.price, result.in_stock, result.currency,
            )
            if not item_id:
                await interaction.followup.send(
                    "This link is already in your tracking list.", ephemeral=True
                )
                return

            if result.price is not None:
                db.add_price_history(item_id, result.price)
            price_str = feature.converter.format_with_conversions(result.price, result.currency)
            stock_label = (
                "Unknown" if result.in_stock is None
                else ("Yes" if result.in_stock else "No")
            )
            display_name = f"**{result.title}**" if result.title else f"**{url}**"
            await interaction.followup.send(
                f"✅ Added: {display_name}\n"
                f"Current price: `{price_str}` | In stock: `{stock_label}`",
                ephemeral=True,
                suppress_embeds=True,
            )

        @self.tree.command(name="wishlist-item-delete", description="Remove a link from tracking")
        @app_commands.describe(url="The URL to remove")
        async def scrape_item_delete(interaction: discord.Interaction, url: str):
            logger.info(f"Command /wishlist-item-delete called by {interaction.user} for {url}")
            if db.delete_scraped_item(interaction.user.id, url):
                await interaction.response.send_message("Link removed and data cleared.", ephemeral=True)
            else:
                await interaction.response.send_message("Link not found in your list.", ephemeral=True)

        @self.tree.command(
            name="wishlist-target-price",
            description="Alert once when an item's price reaches your target",
        )
        @app_commands.describe(
            url="The tracked item URL",
            price="Notify when the price is at or below this amount",
            currency="Currency for the target price",
        )
        @app_commands.choices(currency=CURRENCY_CHOICES)
        async def wishlist_target_price(
            interaction: discord.Interaction,
            url: str,
            price: float,
            currency: app_commands.Choice[str],
        ):
            if price <= 0:
                await interaction.response.send_message(
                    "Target price must be greater than zero.", ephemeral=True
                )
                return
            if db.set_scraped_item_target(
                interaction.user.id, url, price, currency.value
            ):
                await interaction.response.send_message(
                    f"🎯 I will notify you once when this item reaches "
                    f"{price:.2f} {currency.value} or less.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "That URL is not in your tracking list.", ephemeral=True
                )

        @self.tree.command(
            name="wishlist-target-clear",
            description="Remove a target-price alert from a tracked item",
        )
        @app_commands.describe(url="The tracked item URL")
        async def wishlist_target_clear(interaction: discord.Interaction, url: str):
            if db.set_scraped_item_target(interaction.user.id, url, None, None):
                await interaction.response.send_message(
                    "Target-price alert removed.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "That URL is not in your tracking list.", ephemeral=True
                )

        @self.tree.command(
            name="wishlist-restock-only",
            description="Choose whether an item only notifies when restocked",
        )
        @app_commands.describe(
            url="The tracked item URL",
            enabled="When enabled, price and target alerts are suppressed",
        )
        async def wishlist_restock_only(
            interaction: discord.Interaction, url: str, enabled: bool
        ):
            if db.set_scraped_item_restock_only(interaction.user.id, url, enabled):
                text = (
                    "Restock-only mode enabled. Price history still updates, "
                    "but only a back-in-stock notification will be sent."
                    if enabled
                    else "Restock-only mode disabled. Price and target alerts are enabled again."
                )
                await interaction.response.send_message(text, ephemeral=True)
            else:
                await interaction.response.send_message(
                    "That URL is not in your tracking list.", ephemeral=True
                )

        @self.tree.command(
            name="wishlist-refresh",
            description="Refresh one item by URL, or all items when omitted",
        )
        @app_commands.describe(url="Optional tracked URL; omit to refresh your whole wishlist")
        async def wishlist_refresh(
            interaction: discord.Interaction, url: Optional[str] = None
        ):
            single_item = url is not None
            if single_item:
                item = db.get_scraped_item(interaction.user.id, url)
                items = [item] if item is not None else []
            else:
                items = db.get_user_scraped_items_for_refresh(interaction.user.id)
            if not items:
                message = (
                    "That URL is not in your tracking list."
                    if single_item
                    else "You are not tracking any items."
                )
                await interaction.response.send_message(
                    message, ephemeral=True
                )
                return

            now = time.monotonic()
            eligible_items = []
            cooldowns = []
            for item in items:
                key = (interaction.user.id, item[2])
                previous = feature._manual_refresh_at.get(key)
                remaining = (
                    MANUAL_REFRESH_COOLDOWN_SECONDS - (now - previous)
                    if previous is not None
                    else 0.0
                )
                if remaining > 0:
                    cooldowns.append(remaining)
                    continue
                feature._manual_refresh_at[key] = now
                eligible_items.append(item)

            if not eligible_items:
                message = (
                    "That item is still on cooldown."
                    if single_item
                    else "All your tracked items are still on cooldown."
                )
                await interaction.response.send_message(
                    f"{message} Try again in up to {int(max(cooldowns)) + 1}s.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True)
            results = []
            for index, item in enumerate(eligible_items):
                try:
                    result = await feature._manual_refresh_item(item)
                    # Keep a pathological product title or URL from making a
                    # whole Discord follow-up exceed its 2,000-character cap.
                    if len(result) > 1600:
                        result = f"{result[:1597]}..."
                    results.append(result)
                except Exception:
                    # A malformed row or unexpected scraper failure must not
                    # prevent later owned items from being refreshed.
                    logger.exception("Manual refresh failed for %s", item[2])
                    results.append(f"Refresh failed unexpectedly: {_domain(item[2])}")
                if index < len(eligible_items) - 1:
                    await asyncio.sleep(feature.SCRAPE_LOOP_GAP_SECONDS)

            header = (
                "Refreshed the requested item."
                if single_item
                else f"Refreshed {len(eligible_items)} of {len(items)} tracked item(s)."
            )
            if cooldowns:
                header += (
                    f" Skipped {len(cooldowns)} item(s) still on cooldown "
                    f"(up to {int(max(cooldowns)) + 1}s remaining)."
                )
            chunks = []
            current = header
            for result in results:
                block = f"\n\n{result}"
                if len(current) + len(block) > 1900:
                    chunks.append(current)
                    current = result
                else:
                    current += block
            chunks.append(current)
            for chunk in chunks:
                await interaction.followup.send(
                    chunk, ephemeral=True, suppress_embeds=True
                )

        @self.tree.command(
            name="wishlist-show", description="Show your tracked items and their current prices"
        )
        @app_commands.describe(
            currency="Convert every row to this currency (default: show each item in its own currency)"
        )
        @app_commands.choices(currency=CURRENCY_CHOICES)
        async def scrape_show(
            interaction: discord.Interaction,
            currency: Optional[app_commands.Choice[str]] = None,
        ):
            # `target_currency = None` → render each item in its own native
            # (stored / TLD-derived) currency. When the user explicitly picks
            # a currency from the dropdown, every row is converted into it.
            target_currency = currency.value if currency else None
            logger.info(
                f"Command /wishlist-show called by {interaction.user} "
                f"(currency={target_currency or 'native'})"
            )
            await interaction.response.defer(ephemeral=True)

            items = db.get_user_scraped_items_with_settings(interaction.user.id)
            if not items:
                await interaction.followup.send("You are not tracking any items.", ephemeral=True)
                return

            blocks = []
            for (
                url, price, stock, title, item_currency, alert_price,
                alert_currency, restock_only, last_checked_at, check_status,
            ) in items:
                if stock is None:
                    status = "❓ Stock unknown"
                elif stock:
                    status = "✅ In stock"
                else:
                    status = "❌ Out of stock"
                # Resolve currency the same way the scraper does: prefer the
                # value persisted at scrape-time, fall back to a TLD guess.
                source_currency = _effective_currency(item_currency, url)
                price_display = feature._format_show_price(
                    price, source_currency, target_currency
                )
                item_name = f"**{title}**" if title else f"🔗 {url}"
                target = (
                    f"🎯 Target: {alert_price:.2f} {alert_currency}"
                    if alert_price is not None and alert_currency
                    else "🎯 Target: none"
                )
                mode = " | 🔕 Restock-only" if restock_only else ""
                freshness = feature._format_check_status(last_checked_at, check_status)
                blocks.append(
                    f"{item_name}\nSource: {_domain(url)} | URL: {url}\n"
                    f"💰 Price: {price_display} | {status}{mode}\n"
                    f"{target}\n{freshness}"
                )

            # Group items into chunks under Discord's 2000-char message limit.
            header = (
                f"**Your tracked items** (converted to **{target_currency}**):\n\n"
                if target_currency
                else "**Your tracked items** (shown in each item's native currency):\n\n"
            )
            chunks = []
            current = header
            for block in blocks:
                if len(current) + len(block) + 2 > 1900:
                    chunks.append(current.strip())
                    current = block + "\n\n"
                else:
                    current += block + "\n\n"
            if current.strip():
                chunks.append(current.strip())

            for chunk in chunks:
                await interaction.followup.send(chunk, ephemeral=True, suppress_embeds=True)

        @self.tree.command(
            name="wishlist-graph", description="Generate a price history graph for a tracked item"
        )
        @app_commands.describe(
            url="The URL of the item",
            currency="Display currency (default: the item's own currency)",
            days="Days of saved history, 1–180 (default 180)",
        )
        @app_commands.choices(currency=CURRENCY_CHOICES)
        async def scrape_graph(
            interaction: discord.Interaction,
            url: str,
            currency: Optional[app_commands.Choice[str]] = None,
            days: app_commands.Range[int, 1, GRAPH_MAX_DAYS] = GRAPH_MAX_DAYS,
        ):
            item = db.get_scraped_item(interaction.user.id, url)
            target_currency = (
                currency.value if currency else
                _effective_currency(item[6], url) if item else None
            ) or CurrencyConverter.DEFAULT_DISPLAY_CURRENCY
            await send_graph(
                interaction, url=url, currency=target_currency, days=days,
                percentage=False, converter=feature.converter,
                effective_currency=_effective_currency,
            )

        @self.tree.command(
            name="wishlist-graph-all",
            description="Combined price history graph for ALL your tracked items",
        )
        @app_commands.describe(
            currency="Display currency (default: majority currency across your items)",
            days="Days of saved history, 1–180 (default 180)",
            percentage="Compare percentage changes instead of prices (default false)",
        )
        @app_commands.choices(currency=CURRENCY_CHOICES)
        async def scrape_graph_all(
            interaction: discord.Interaction,
            currency: Optional[app_commands.Choice[str]] = None,
            days: app_commands.Range[int, 1, GRAPH_MAX_DAYS] = GRAPH_MAX_DAYS,
            percentage: bool = False,
        ):
            items = db.get_user_scraped_items(interaction.user.id)
            target_currency = currency.value if currency else _majority_currency(
                (url, stored_currency) for url, _p, _s, _t, stored_currency in items
            )
            await send_graph(
                interaction, url=None, currency=target_currency, days=days,
                percentage=percentage, converter=feature.converter,
                effective_currency=_effective_currency,
            )

    def _format_show_price(
        self,
        price,
        source_currency: str | None,
        target_currency: str | None,
    ) -> str:
        """Render the price column for one `/wishlist-show` row.

        - `target_currency=None`        → show in `source_currency` as-is.
        - `target_currency=<picked>`    → convert source → target via
                                          `CurrencyConverter.format_in_currency`.
        Both paths fall back gracefully on missing data ("N/A" / "(?)").
        """
        if price is None:
            return "N/A"
        try:
            price = float(price)
        except (ValueError, TypeError):
            return "N/A"

        if target_currency:
            return f"`{self.converter.format_in_currency(price, source_currency, target_currency)}`"

        if source_currency:
            return f"`{price:.2f} {source_currency.upper()}`"
        return f"`{price:.2f} (?)`"

    @staticmethod
    def _format_check_status(
        checked_at: float | None, status: str | None
    ) -> str:
        if not checked_at:
            return "Last checked: not yet"
        try:
            when = datetime.fromtimestamp(float(checked_at)).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError, OSError):
            return "Last checked: unknown"
        labels = {
            "ok": "OK",
            FAILURE_BLOCKED: "source blocked/unreachable",
            FAILURE_UNSUPPORTED: "source unsupported",
        }
        return f"Last checked: {when} ({labels.get(status, status or 'unknown')})"

    def _target_price_reached(
        self,
        current_price: float | None,
        source_currency: str | None,
        target_price: float | None,
        target_currency: str | None,
    ) -> tuple[bool | None, float | None]:
        """Return whether a configured currency-aware target has been met."""
        if current_price is None or target_price is None or not target_currency:
            return None, None
        converted = self.converter.to_currency(
            current_price, source_currency, target_currency
        )
        if converted is None:
            return None, None
        return converted <= target_price, converted

    async def _manual_refresh_item(self, item) -> str:
        """Fetch one owned item and return a direct, non-DM status."""
        (
            item_id, _user_id, url, old_price, old_stock, old_title,
            old_currency, _old_alert_kind, _old_alert_price, target_price,
            target_currency, _target_alerted, _restock_only, _last_checked,
            _last_check_status,
        ) = item
        result = await asyncio.to_thread(refresh_item, item, self.scraper)
        status = result.failure or "ok"
        await record("processing", "wishlist-check", scope_type="global")

        if result.failure == FAILURE_BLOCKED:
            await record("failure", "wishlist-check", scope_type="global")
            return (
                f"Refresh failed: {_domain(url)} blocked or could not be reached.\n"
                f"{self._format_check_status(time.time(), status)}"
            )
        if result.failure == FAILURE_UNSUPPORTED and not result.has_data:
            await record("failure", "wishlist-check", scope_type="global")
            return (
                f"Refresh failed: {_domain(url)} returned no supported price/stock data.\n"
                f"{self._format_check_status(time.time(), status)}"
            )

        title = result.title or old_title or url
        source_currency = _effective_currency(result.currency or old_currency, url)
        price_display = self._format_show_price(
            result.price if result.price is not None else old_price,
            source_currency,
            None,
        )
        stock = result.in_stock if result.in_stock is not None else old_stock
        stock_label = (
            "Stock unknown" if stock is None
            else ("In stock" if stock else "Out of stock")
        )
        lines = [
            f"Refreshed: {title}",
            f"Source: {_domain(url)}",
            f"Price: {price_display} | {stock_label}",
        ]
        reached, converted = self._target_price_reached(
            result.price if result.price is not None else old_price,
            source_currency,
            target_price,
            target_currency,
        )
        if target_price is not None and target_currency:
            if reached is True:
                lines.append(
                    f"Target reached: {converted:.2f} {target_currency} "
                    f"<= {target_price:.2f} {target_currency}"
                )
            elif reached is False:
                lines.append(
                    f"Target: {target_price:.2f} {target_currency} (not reached)"
                )
            else:
                lines.append(
                    f"Target: {target_price:.2f} {target_currency} "
                    f"(conversion unavailable)"
                )
        lines.append(self._format_check_status(time.time(), status))
        return "\n".join(lines)

    @staticmethod
    def _render_price_graph(
        timestamps, prices, title, item_currency, target_price=None
    ) -> discord.File:
        """Render a modern single-item Vega-Lite chart entirely in memory."""
        png = render_price_history_png(
            timestamps, prices, title, item_currency, target_price
        )
        return discord.File(io.BytesIO(png), filename="price_history.png")

    @staticmethod
    def _render_multi_price_graph(
        series: list[tuple[str, list[tuple[datetime, float]]]],
        currency_label: str,
    ) -> discord.File:
        """Render an all-items Vega-Lite chart entirely in memory."""
        png = render_multi_price_history_png(series, currency_label)
        return discord.File(io.BytesIO(png), filename="price_history_all.png")

    # Politeness delay between item fetches so we're not hammering a host
    # when a user has multiple URLs on the same domain in one pass.
    SCRAPE_LOOP_GAP_SECONDS = 1.0

    async def _process_scrape_item(self, item) -> None:
        """Run one full per-item scrape: fetch → diff → classify → DM → persist.

        Raises nothing on its own — `_scrape_loop` wraps the whole call in a
        catch-all so an unexpected error in any single item is logged and
        skipped, never killing the whole 12-hour pass.
        """
        (
            item_id, user_id, url,
            old_price, old_stock_status, old_title, old_currency,
            old_alert_kind, old_alert_price,
        ) = item[:9]
        # Older unit tests and pre-migration callers retain the original
        # nine fields. Missing preferences safely mean legacy behaviour.
        target_price = item[9] if len(item) > 9 else None
        target_currency = item[10] if len(item) > 10 else None
        target_alerted = bool(item[11]) if len(item) > 11 else False
        restock_only = bool(item[12]) if len(item) > 12 else False

        # Each `fetch` is up to ~15s of blocking I/O. Running it in a worker
        # thread keeps the bot responsive to slash commands and messages
        # during the scrape pass.
        result = await asyncio.to_thread(self.scraper.fetch, url)
        db.update_scraped_item_check_status(item_id, result.failure or "ok")
        await record("processing", "wishlist-check", scope_type="global")

        # Transport-level failure (timeout, anti-bot block, 5xx): trust
        # nothing, change nothing. Try again next pass.
        if result.failure == FAILURE_BLOCKED:
            await record("failure", "wishlist-check", scope_type="global")
            return
        # Page reachable but literally nothing useful was parsed — same
        # outcome. Don't overwrite known-good state with empty data.
        if result.failure == FAILURE_UNSUPPORTED and not result.has_data:
            await record("failure", "wishlist-check", scope_type="global")
            return

        # Detect what changed against the previously-persisted state.
        #   - `price_changed`  requires both an old and a new price.
        #   - `back_in_stock`  requires that we *previously knew* it was OOS
        #     (old_stock_status is a concrete 0, not NULL/unknown) and that
        #     we *now know* it's in stock (result.in_stock is literally True,
        #     not None).
        price_changed = (
            old_price is not None
            and result.price is not None
            and result.price != old_price
        )
        back_in_stock = (
            old_stock_status is not None
            and not old_stock_status
            and result.in_stock is True
        )

        # Classify the new price against historical data BEFORE we insert the
        # fresh row. `_classify_price` excludes `current` from its input on
        # the caller's behalf, so feeding it the not-yet-updated history is
        # the cleanest way to compare against actually-prior values.
        prior_history = db.get_price_history(user_id, url)
        prior_prices = [row[0] for row in prior_history]
        decision = _classify_price(
            current=result.price,
            history=prior_prices,
            last_alert_kind=old_alert_kind,
            last_alert_price=old_alert_price,
        )
        source_currency = _effective_currency(result.currency or old_currency, url)
        target_reached, converted_target_price = self._target_price_reached(
            result.price,
            source_currency,
            target_price,
            target_currency,
        )
        target_alert = target_reached is True and not target_alerted
        if target_reached is not None and target_reached != target_alerted:
            db.update_scraped_item_target_state(item_id, target_reached)

        if result.price is not None:
            db.add_price_history(item_id, result.price)

        should_notify = (
            back_in_stock
            if restock_only
            else price_changed or back_in_stock or decision.alert_kind or target_alert
        )
        if should_notify:
            try:
                user = await self.client.fetch_user(user_id)
                if user:
                    disp_name = result.title or old_title or url
                    msg = f"🔔 **Update: {disp_name}**\nLink: {url}\n"
                    if back_in_stock:
                        msg += "✅ Item is now **BACK IN STOCK**!\n"
                    if price_changed:
                        # Apply the same TLD currency fallback `/wishlist-show`
                        # uses, so old rows with currency = NULL render in the
                        # right unit instead of as a bare number.
                        old_src = _effective_currency(old_currency, url)
                        new_src = _effective_currency(result.currency, url)
                        old_str = self.converter.format_with_conversions(old_price, old_src)
                        new_str = self.converter.format_with_conversions(result.price, new_src)
                        msg += f"💰 Price changed: `{old_str}` -> **{new_str}**\n"
                        try:
                            llm_msg = await asyncio.to_thread(
                                generate_price_change_message,
                                disp_name,
                                old_price,
                                result.price,
                                old_str,
                                new_str,
                            )
                            if llm_msg:
                                msg += f"🤖 *{llm_msg}*\n"
                        except Exception:
                            # Creative commentary is optional. The exact price
                            # notification must still be sent if generation has
                            # an unexpected failure.
                            logger.exception(
                                f"Unexpected LLM price-message failure for {url}"
                            )
                    if decision.alert_kind:
                        new_src = _effective_currency(result.currency, url)
                        alert_text = self._format_alert_section(decision, new_src)
                        msg += alert_text
                    if target_alert and target_price is not None and target_currency:
                        msg += (
                            f"Target reached. Now "
                            f"{converted_target_price:.2f} {target_currency} "
                            f"(target: {target_price:.2f} {target_currency}).\n"
                        )
                    await user.send(msg, suppress_embeds=True)
                    await record("scheduled", "wishlist-notification", scope_type="dm")
                    logger.info(
                        f"Scrape DM sent to user {user_id} for {url} "
                        f"(price_changed={price_changed}, back_in_stock={back_in_stock}, "
                        f"alert={decision.alert_kind}, target_alert={target_alert}, "
                        f"restock_only={restock_only})"
                    )
            except Exception as e:
                logger.error(f"Could not send DM to user {user_id}: {e}")
                await record("failure", "wishlist-notification", scope_type="dm")

        # Persist the latest price / stock / title / currency snapshot. COALESCE
        # inside the SQL means passing None for any field leaves the previous
        # value intact — so a price-less stock-only scrape doesn't wipe the
        # last-known price, and an unknown stock read doesn't flip the status
        # to OOS.
        db.update_scraped_item_status(
            item_id, result.price, result.in_stock, result.title, result.currency,
        )

        # Persist the new alert zone unconditionally — even when no DM fires
        # we want the state to reflect the current zone so the NEXT pass's
        # transition logic is accurate (e.g. "we were in high, dropped to
        # neutral → now armed to re-alert on the next HIGH crossing").
        if (
            decision.new_state != old_alert_kind
            or decision.new_state_price != old_alert_price
        ):
            db.update_item_alert_state(
                item_id, decision.new_state, decision.new_state_price,
            )
    def _format_alert_section(self, decision: "AlertDecision", source_currency: str | None) -> str:
        """Render the LOW/HIGH alert block of the per-item DM.

        Pulled out of `_process_scrape_item` so the formatting (and the
        currency-fallback dance) is in one place. Returns a `\\n`-terminated
        markdown block ready to be appended to the main DM string.
        """
        if decision.alert_kind == "low":
            # Re-alert (price drops further) vs first entry to the low zone:
            # the former has `prev_alert_price` set, so we can show the
            # delta; the latter just says "new all-time low".
            current_str = self.converter.format_with_conversions(
                decision.new_state_price, source_currency,
            )
            prev_low_str = self.converter.format_with_conversions(
                decision.all_time_low, source_currency,
            )
            if decision.prev_alert_price is not None and decision.prev_alert_price > (decision.new_state_price or 0):
                # We've already alerted at a higher floor in this low period.
                prev_alert_str = self.converter.format_with_conversions(
                    decision.prev_alert_price, source_currency,
                )
                return (
                    f"🟢 **New low!** Now `{current_str}` — even lower than the "
                    f"previous alert at `{prev_alert_str}`. **Buy window.**\n"
                )
            return (
                f"🟢 **All-time low!** Now `{current_str}` — matches/beats the "
                f"previous low of `{prev_low_str}` over the tracked window. "
                f"**Buy window.**\n"
            )
        if decision.alert_kind == "high":
            current_str = self.converter.format_with_conversions(
                decision.new_state_price, source_currency,
            )
            median_str = self.converter.format_with_conversions(
                decision.median_price, source_currency,
            )
            return (
                f"🔴 **Above usual.** Now `{current_str}` — historical median "
                f"is `{median_str}`. **Maybe wait.**\n"
            )
        return ""

    @tasks.loop(hours=12)
    async def _scrape_loop(self):
        logger.info("Starting scheduled price scrape task...")
        items = db.get_all_scraped_items()

        for item in items:
            try:
                await self._process_scrape_item(item)
            except Exception:
                # Catch-all so one bad row never kills the whole pass. Logged
                # with full traceback + URL context so we can investigate.
                url = item[2] if len(item) > 2 else "<unknown>"
                logger.exception(f"Unexpected error scraping {url}; skipping")
                await record("failure", "wishlist-check", scope_type="global")
            # Politeness sleep between fetches — even with `to_thread` we
            # don't want to flood a host that has multiple tracked URLs in
            # one pass. The bot's event loop stays responsive during the
            # await regardless.
            await asyncio.sleep(self.SCRAPE_LOOP_GAP_SECONDS)

        db.clean_old_price_history(days=PRICE_HISTORY_RETENTION_DAYS)
        logger.info(
            f"Finished price scrape task and cleaned history "
            f"(retention: {PRICE_HISTORY_RETENTION_DAYS} days)."
        )

    @tasks.loop(hours=24)
    async def _refresh_rates_loop(self):
        if await asyncio.to_thread(self.converter.refresh):
            await record("processing", "exchange-rate-refresh", scope_type="global")
        else:
            await record("failure", "exchange-rate-refresh", scope_type="global")
