import asyncio
import io
import logging
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

import discord
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import requests
from discord import app_commands
from discord.ext import tasks

import db
# Re-export the pure scraping primitives from `scraper.py` so existing
# call-sites and tests can keep importing them from `features.scraping`.
# Splitting them out of this module lets `api.py` reuse `PriceScraper`
# without dragging discord.py + matplotlib into the API process.
from scraper import (  # noqa: F401  (re-exported for back-compat)
    FAILURE_BLOCKED,
    FAILURE_UNSUPPORTED,
    PriceScraper,
    ScrapeResult,
    _domain,
    _is_valid_http_url,
)

logger = logging.getLogger("discord_bot")

# Non-interactive matplotlib backend, safe inside an async bot process.
plt.switch_backend("Agg")

# Size of the rolling per-item price-history window. At every scrape pass
# the loop trims rows older than this many days, so each tracked item
# always shows roughly the most recent N days of history regardless of how
# long it's been tracked — items are NOT wiped on their 180-day anniversary,
# the window just slides forward one pass at a time.
#
# At a 12 h scrape cadence and 100 tracked items this is ~36 k rows /
# ~2.5 MB at steady state — fine for the Pi. Backed by
# `idx_price_history_timestamp` so the periodic cleanup stays fast as
# the table grows.
PRICE_HISTORY_RETENTION_DAYS = 180


# ---------------------------------------------------------------------------
# Buy-signal alerts (LOW / HIGH)
# ---------------------------------------------------------------------------
#
# Evaluated once per scrape pass per item inside `_process_scrape_item`.
# Goal: DM the user when the price drops to a new all-time low ("buy now")
# or rises above the historical median ("maybe wait"), without spamming
# them on every 12 h pass while the price stays in the alert zone.

# Minimum number of historical price points required before any alert can
# fire. Avoids noise on freshly-added items where 2 points trivially
# define both min and median.
ALERT_MIN_DATA_POINTS = 7

# When already in the LOW zone, only re-alert if the new price is at least
# this much lower than the price at the previous LOW alert. Prevents
# penny-fluctuation spam while still firing on a meaningfully fresher low.
ALERT_LOW_REALERT_DROP_PCT = 0.01  # 1 %


@dataclass
class AlertDecision:
    """Result of running `_classify_price` for one scrape pass.

    `alert_kind` is what to DM the user about (or None for silence).
    `new_state` / `new_state_price` are what to persist back into
    `scraped_items.last_alert_kind` / `last_alert_price` — they may
    differ from `alert_kind` because we always update the state even
    when we suppress the DM (so future passes know what zone we're in).

    The remaining fields are context for message formatting (all-time
    low, median, previous-alert price) — populated even when no alert
    fires, so callers don't have to recompute them.
    """

    alert_kind: str | None              # "low" | "high" | None
    new_state: str | None               # "low" | "high" | None (= neutral)
    new_state_price: float | None
    all_time_low: float | None = None
    median_price: float | None = None
    prev_alert_price: float | None = None


def _classify_price(
    current: float | None,
    history: list[float | None],
    last_alert_kind: str | None,
    last_alert_price: float | None,
) -> AlertDecision:
    """Decide whether this scrape pass should fire a LOW or HIGH alert.

    `current` is the price just scraped (in the item's native currency).
    `history` is the list of historical prices for this item — call sites
    are responsible for excluding `current` from it, so the comparison
    is against actually-prior data.

    The function is pure: no DB, no DM, no side effects. The caller
    decides what to do with the returned `AlertDecision`.

    Zones:
      - "low"     :  current <= min(history)
      - "high"    :  current  > median(history)
      - None      :  neutral (between min and median, inclusive)

    Firing rules:
      - LOW fires when we *enter* the low zone (last_alert_kind != "low")
        OR when we're already in it and the new price is at least
        `ALERT_LOW_REALERT_DROP_PCT` lower than the previous LOW alert.
      - HIGH fires only when we *enter* the high zone (one alert per
        elevated period; the state resets back to neutral once the
        price drops to/below the median, re-arming the next HIGH alert).

    Guardrails (return silently before any zone logic runs):
      - `current` must not be None.
      - History must hold at least `ALERT_MIN_DATA_POINTS` numeric prices.
      - All observations (history + current) must span a window at least
        `ALERT_LOW_REALERT_DROP_PCT` wide. Otherwise the data is too flat
        for any zone signal to be meaningful — common case is a freshly-
        tracked stable-price item, where without this check the inclusive
        `current <= all_time_low` comparison would fire one LOW alert on
        the first pass to cross the minimum-data-points threshold.
    """
    # Not enough info to say anything.
    if current is None:
        return AlertDecision(None, last_alert_kind, last_alert_price)

    numeric_history = [p for p in history if isinstance(p, (int, float))]
    if len(numeric_history) < ALERT_MIN_DATA_POINTS:
        return AlertDecision(None, last_alert_kind, last_alert_price)

    all_time_low = min(numeric_history)
    median_price = statistics.median(numeric_history)

    # Variance guard: if every observation we've seen (history + the price
    # we just scraped) sits inside a window narrower than the LOW re-alert
    # threshold (~1 %), there isn't enough spread to derive a meaningful
    # zone signal. Suppress alerts in that case.
    #
    # We deliberately include `current` in the spread calculation: a
    # perfectly flat history followed by a real drop should still alert
    # (the drop itself creates the spread), while a perfectly flat
    # history followed by another flat-floor reading should not.
    #
    # This prevents the stable-item false positive where a flat history
    # would otherwise fire one LOW alert per item on the first pass to
    # cross `ALERT_MIN_DATA_POINTS` (or right after a rollout that adds
    # the alerts feature to existing rows with stale prices).
    all_observations = numeric_history + [current]
    observed_max = max(all_observations)
    observed_min = min(all_observations)
    if observed_max < observed_min * (1 + ALERT_LOW_REALERT_DROP_PCT):
        return AlertDecision(
            None, last_alert_kind, last_alert_price,
            all_time_low=all_time_low,
            median_price=median_price,
            prev_alert_price=last_alert_price,
        )

    # Which zone is `current` in right now?
    if current <= all_time_low:
        zone = "low"
    elif current > median_price:
        zone = "high"
    else:
        zone = None  # neutral

    alert: str | None = None
    new_state_price = last_alert_price  # default: preserve unless we update below

    if zone == "low":
        if last_alert_kind != "low":
            # Just entered the low zone (was neutral or high) — fire.
            alert = "low"
            new_state_price = current
        elif (
            last_alert_price is not None
            and current <= last_alert_price * (1 - ALERT_LOW_REALERT_DROP_PCT)
        ):
            # Already at the low, but the new price beats the previous
            # alert by a meaningful margin → re-fire so the user knows
            # the floor has dropped further.
            alert = "low"
            new_state_price = current
        # else: still at the low, no meaningful further drop → silent.

    elif zone == "high":
        if last_alert_kind != "high":
            # Just entered the high zone (was neutral or low) — fire.
            alert = "high"
            new_state_price = current
        # else: already alerted on this elevated period → silent.
        # State only re-arms when the price drops back to/below median.

    else:  # neutral
        # No alert and no remembered alert price — leaving the zone
        # re-arms both LOW and HIGH for the next time we re-enter them.
        new_state_price = None

    return AlertDecision(
        alert_kind=alert,
        new_state=zone,
        new_state_price=new_state_price,
        all_time_low=all_time_low,
        median_price=median_price,
        prev_alert_price=last_alert_price,
    )


# ---------------------------------------------------------------------------
# Currency conversion
# ---------------------------------------------------------------------------


class CurrencyConverter:
    """Keeps a fresh table of exchange rates in DB and converts between them.

    All rates are stored relative to DKK, so to go from A → B we first convert
    A → DKK then DKK → B.
    """

    EXCHANGE_API = "https://open.er-api.com/v6/latest/EUR"
    # Rates we actively fetch and persist. DKK and EUR are always set during
    # `refresh()`; these are the additional ones we pull from the API.
    TARGET_CURRENCIES = ("RON", "USD", "GBP")
    # Currencies offered as `/wishlist-*` display options. Must be a subset of the
    # currencies we have rates for (i.e. DKK, EUR, plus everything in
    # TARGET_CURRENCIES).
    SUPPORTED_DISPLAY_CURRENCIES = ("RON", "DKK", "EUR", "USD", "GBP")
    DEFAULT_DISPLAY_CURRENCY = "RON"

    def refresh(self) -> None:
        logger.info("Starting scheduled exchange rate update task...")
        try:
            response = requests.get(self.EXCHANGE_API, timeout=10)
            response.raise_for_status()
            data = response.json()
            rates = data.get("rates")
            if not rates or "DKK" not in rates:
                logger.error("Failed to fetch DKK rate from exchange rate API.")
                return

            eur_to_dkk = rates["DKK"]
            db.set_exchange_rate("DKK", 1.0)
            db.set_exchange_rate("EUR", eur_to_dkk)
            for currency in self.TARGET_CURRENCIES:
                if currency in rates:
                    currency_to_dkk = eur_to_dkk / rates[currency]
                    db.set_exchange_rate(currency, currency_to_dkk)
            logger.info("Exchange rates updated successfully.")
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch exchange rates from API: {e}")
        except Exception:
            logger.exception("Error in update_exchange_rates_task")

    def convert(self, price, from_currency, to_currency):
        if price is None or not from_currency or not to_currency:
            return None
        try:
            price = float(price)
        except (ValueError, TypeError):
            return None

        from_rate = db.get_exchange_rate(from_currency)
        to_rate = db.get_exchange_rate(to_currency)
        if not from_rate or not to_rate:
            return None

        price_in_dkk = price * from_rate
        return price_in_dkk / to_rate

    def to_currency(self, price, source_currency, target_currency) -> float | None:
        """Best-effort conversion of `price` from source to target. Returns the
        converted float or None when conversion isn't possible (unknown source,
        missing rates, non-numeric input)."""
        if price is None or not source_currency or not target_currency:
            return None
        try:
            price = float(price)
        except (ValueError, TypeError):
            return None
        if source_currency.upper() == target_currency.upper():
            return price
        return self.convert(price, source_currency, target_currency)

    def format_in_currency(self, price, source_currency, target_currency) -> str:
        """Format `price` (in `source_currency`) as a display string in
        `target_currency`. Falls back gracefully when conversion isn't
        possible:

        - Unknown source currency  → "<price> (?)"   (we don't know the unit)
        - Missing exchange rate    → "<price> SRC*"  (asterisk = unconverted)
        - Same source as target    → "<price> TGT"
        - Successful conversion    → "<converted> TGT"
        """
        if price is None:
            return "N/A"
        try:
            price = float(price)
        except (ValueError, TypeError):
            return "N/A"

        target = target_currency.upper()
        if not source_currency:
            return f"{price:.2f} (?)"
        source = source_currency.upper()
        if source == target:
            return f"{price:.2f} {target}"
        converted = self.convert(price, source, target)
        if converted is None:
            return f"{price:.2f} {source}*"
        return f"{converted:.2f} {target}"

    def format_with_conversions(self, price, currency) -> str:
        if price is None:
            return "N/A"
        try:
            price = float(price)
        except (ValueError, TypeError):
            return "N/A"

        base_str = f"{price:.2f} {currency}" if currency else str(price)
        if not currency:
            return base_str

        conversions = []
        for target in ("DKK", "EUR", "USD"):
            if currency.upper() != target:
                val = self.convert(price, currency, target)
                if val:
                    conversions.append(f"{val:.2f} {target}")

        if conversions:
            return f"{base_str} (~" + " | ".join(conversions) + ")"
        return base_str


# ---------------------------------------------------------------------------
# Slash-command currency picker + Discord-side currency helpers
# ---------------------------------------------------------------------------


# Slash-command currency picker. Built from `CurrencyConverter`'s supported
# set so this stays in sync with the conversion code automatically.
CURRENCY_CHOICES = [
    app_commands.Choice(name=c, value=c)
    for c in CurrencyConverter.SUPPORTED_DISPLAY_CURRENCIES
]


def _effective_currency(stored_currency: str | None, url: str) -> str | None:
    """Pick the currency to display a tracked item in: the value persisted by
    `PriceScraper.fetch`, or — if that's missing — the TLD-based guess (`.dk`
    → DKK, `.ro` → RON, …).

    Lets DB rows that pre-date the TLD fallback in `PriceScraper.fetch` still
    render and convert correctly in `/wishlist-show`, `/wishlist-graph`, and
    `/wishlist-graph-all` without needing a migration."""
    return stored_currency or PriceScraper._currency_from_tld(url)


def _majority_currency(url_currency_pairs) -> str:
    """Pick the most-common effective currency across `(url, stored_currency)`
    pairs.

    Each pair is run through `_effective_currency` first so the TLD fallback
    counts (a `.ro` row with NULL currency tallies as RON). Ties are broken
    by insertion order — the first currency to reach the max wins.

    Returns `CurrencyConverter.DEFAULT_DISPLAY_CURRENCY` (RON) when none of
    the pairs have a known currency. Used as the default for
    `/wishlist-graph-all` (over the user's full list) so the chart shows the
    largest number of items without conversion."""
    counts: Counter[str] = Counter()
    for url, stored in url_currency_pairs:
        eff = _effective_currency(stored, url)
        if eff:
            counts[eff.upper()] += 1
    if not counts:
        return CurrencyConverter.DEFAULT_DISPLAY_CURRENCY
    return counts.most_common(1)[0][0]


# ---------------------------------------------------------------------------
# Feature wiring
# ---------------------------------------------------------------------------


class ScrapingFeature:
    """Owns all price-tracking commands, exchange rate refreshes, and the
    periodic price-scrape DM task."""

    def __init__(self, client: discord.Client, tree: app_commands.CommandTree):
        self.client = client
        self.tree = tree
        self.converter = CurrencyConverter()
        self.scraper = PriceScraper()
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
            name="wishlist-show", description="Show your tracked items and their current prices"
        )
        @app_commands.describe(
            currency="Convert every row to this currency (default: show each item in its own currency)"
        )
        @app_commands.choices(currency=CURRENCY_CHOICES)
        async def scrape_show(
            interaction: discord.Interaction,
            currency: app_commands.Choice[str] | None = None,
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

            items = db.get_user_scraped_items(interaction.user.id)
            if not items:
                await interaction.followup.send("You are not tracking any items.", ephemeral=True)
                return

            blocks = []
            for url, price, stock, title, item_currency in items:
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
                blocks.append(f"{item_name}\nURL: {url}\n💰 Price: {price_display} | {status}")

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
        )
        @app_commands.choices(currency=CURRENCY_CHOICES)
        async def scrape_graph(
            interaction: discord.Interaction,
            url: str,
            currency: app_commands.Choice[str] | None = None,
        ):
            await interaction.response.defer(ephemeral=True)

            history = db.get_price_history(interaction.user.id, url)
            if not history:
                await interaction.followup.send(
                    "No price history found for this URL in your list.", ephemeral=True
                )
                return

            title = history[0][2] or "Price History"

            item_info = next(
                (item for item in db.get_user_scraped_items(interaction.user.id) if item[0] == url),
                None,
            )
            stored_currency = (
                item_info[4] if item_info and len(item_info) > 4 else None
            )
            item_currency = _effective_currency(stored_currency, url)

            # Default to the item's own currency when the user didn't ask
            # otherwise — graphing one item in some other currency is just
            # unnecessary conversion noise. Fall back to RON only if the
            # item's currency can't be determined at all.
            if currency:
                target_currency = currency.value
                auto_chosen = False
            else:
                target_currency = item_currency or CurrencyConverter.DEFAULT_DISPLAY_CURRENCY
                auto_chosen = True

            logger.info(
                f"Command /wishlist-graph called by {interaction.user} for {url} "
                f"(currency={target_currency}{' [auto]' if auto_chosen else ''})"
            )

            # Convert each point to the requested display currency. If a point
            # can't be converted we drop it — better an honest gap than a misleading
            # number labelled in the wrong unit.
            #
            # Timestamps are kept as real `datetime` objects (not pre-formatted
            # strings) so matplotlib treats the x-axis as a true time axis.
            # That lets `ConciseDateFormatter` in `_render_price_graph` pick
            # readable labels regardless of range — hours within a day, days
            # within a month, months across half a year.
            timestamps: list[datetime] = []
            prices: list[float] = []
            for raw_price, ts, _ in history:
                converted = feature.converter.to_currency(
                    raw_price, item_currency, target_currency
                )
                if converted is None:
                    continue
                timestamps.append(datetime.fromtimestamp(ts))
                prices.append(converted)

            if not prices:
                await interaction.followup.send(
                    f"Couldn't render this graph in **{target_currency}** — the item's "
                    f"currency is unknown or no exchange rate is available. "
                    f"Try a different currency.",
                    ephemeral=True,
                )
                return

            file = feature._render_price_graph(timestamps, prices, title, target_currency)
            await interaction.followup.send(file=file, ephemeral=True)

        @self.tree.command(
            name="wishlist-graph-all",
            description="Combined price history graph for ALL your tracked items",
        )
        @app_commands.describe(
            currency="Display currency (default: the majority currency across your tracked items)"
        )
        @app_commands.choices(currency=CURRENCY_CHOICES)
        async def scrape_graph_all(
            interaction: discord.Interaction,
            currency: app_commands.Choice[str] | None = None,
        ):
            await interaction.response.defer(ephemeral=True)

            items = db.get_user_scraped_items(interaction.user.id)
            if not items:
                await interaction.followup.send(
                    "You are not tracking any items.", ephemeral=True
                )
                return

            # Default to the majority currency across the user's items, so the
            # chart can show as many items as possible without conversion. The
            # user can still override with the dropdown.
            if currency:
                target_currency = currency.value
                auto_chosen = False
            else:
                target_currency = _majority_currency(
                    (url, stored_currency) for url, _p, _s, _t, stored_currency in items
                )
                auto_chosen = True

            logger.info(
                f"Command /wishlist-graph-all called by {interaction.user} "
                f"(currency={target_currency}{' [auto]' if auto_chosen else ''})"
            )

            # Build one (label, [(datetime, price_in_target), ...]) series per item.
            # All series share a single Y-axis in `target_currency` so
            # cross-currency comparisons are valid.
            series: list[tuple[str, list[tuple[datetime, float]]]] = []
            skipped_no_history = 0
            skipped_no_currency = 0

            for url, _last_price, _stock, title, stored_currency in items:
                history = db.get_price_history(interaction.user.id, url)
                if not history:
                    skipped_no_history += 1
                    continue

                item_currency = _effective_currency(stored_currency, url)
                points: list[tuple[datetime, float]] = []
                for price, ts, _row_title in history:
                    converted = feature.converter.to_currency(
                        price, item_currency, target_currency
                    )
                    if converted is not None:
                        points.append((datetime.fromtimestamp(ts), converted))

                if not points:
                    skipped_no_currency += 1
                    continue

                series.append((title or _domain(url), points))

            if not series:
                await interaction.followup.send(
                    f"No price history available yet for any of your tracked items "
                    f"(in **{target_currency}**).",
                    ephemeral=True,
                )
                return

            file = feature._render_multi_price_graph(series, target_currency)
            parts = [
                f"Combined price history for **{len(series)}** tracked item"
                f"{'s' if len(series) != 1 else ''} (normalized to **{target_currency}**).",
            ]
            if skipped_no_history:
                parts.append(f"_Skipped (no history yet): {skipped_no_history}._")
            if skipped_no_currency:
                parts.append(
                    f"_Skipped (no rate to convert into {target_currency}): "
                    f"{skipped_no_currency}._"
                )
            await interaction.followup.send("\n".join(parts), file=file, ephemeral=True)

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
    def _render_price_graph(timestamps, prices, title, item_currency) -> discord.File:
        """Render a single-item price-evolution chart.

        `timestamps` must be a list of `datetime` objects (not pre-formatted
        strings) so matplotlib treats the x-axis as a real time axis.
        That's what lets `ConciseDateFormatter` adapt the tick labels to
        the visible range — hours within a day, days within a month,
        months across a half-year. Without it, long-range charts crowd
        the axis to the point of being unreadable.
        """
        fig, ax = plt.subplots(figsize=(10, 6), facecolor="#2f3136")
        ax.set_facecolor("#36393f")
        ax.plot(timestamps, prices, marker="o", linestyle="-", color="#7289da", linewidth=2)

        ax.set_title(f"Price Evolution: {title[:50]}", color="white", fontsize=14)
        ax.set_xlabel("Date & Time", color="white")
        ax.set_ylabel(f"Price ({item_currency})", color="white")
        ax.tick_params(axis="x", colors="white")
        ax.tick_params(axis="y", colors="white")
        for spine in ax.spines.values():
            spine.set_color("white")
        ax.grid(True, color="#4f545c", linestyle="--", linewidth=0.5)

        # Auto-pick a date locator + matching concise formatter so the labels
        # stay readable from a single-day range up to multi-month history.
        locator = mdates.AutoDateLocator()
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        # `autofmt_xdate` handles rotation/alignment for whichever ticks the
        # locator picked — replaces the old fixed `rotation=45`.
        fig.autofmt_xdate()
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return discord.File(buf, filename="price_history.png")

    @staticmethod
    def _render_multi_price_graph(
        series: list[tuple[str, list[tuple[datetime, float]]]],
        currency_label: str,
    ) -> discord.File:
        """Render a multi-line price chart.

        `series` is a list of `(label, [(datetime, price), ...])` tuples, one
        per tracked item. All Y-values must already be in the same currency,
        named by `currency_label` (used only for the axis title).
        """
        fig, ax = plt.subplots(figsize=(12, 7), facecolor="#2f3136")
        ax.set_facecolor("#36393f")

        for label, points in series:
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            # Trim long product names so the legend stays readable.
            legend_label = (label[:40] + "…") if len(label) > 40 else label
            ax.plot(
                xs, ys,
                marker="o", linestyle="-", linewidth=2, markersize=4,
                label=legend_label,
            )

        ax.set_title(
            "Price Evolution — All Tracked Items", color="white", fontsize=14,
        )
        ax.set_xlabel("Date & Time", color="white")
        ax.set_ylabel(f"Price ({currency_label})", color="white")
        ax.tick_params(axis="x", colors="white")
        ax.tick_params(axis="y", colors="white")
        for spine in ax.spines.values():
            spine.set_color("white")
        ax.grid(True, color="#4f545c", linestyle="--", linewidth=0.5)
        ax.legend(
            loc="best", facecolor="#36393f", edgecolor="#4f545c",
            labelcolor="white", fontsize=8,
        )

        # Auto-pick a date locator + matching `ConciseDateFormatter` so the
        # axis labels adapt to whatever range the user is looking at — hours
        # within a day, days within a month, months across a half-year —
        # rather than getting crammed at a fixed `"dd/mm HH:MM"` granularity
        # that becomes illegible past ~30 ticks.
        locator = mdates.AutoDateLocator()
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        # `autofmt_xdate` rotates/right-aligns the (now-formatted) labels so
        # they don't overlap on dense ranges.
        fig.autofmt_xdate()
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return discord.File(buf, filename="price_history_all.png")

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
        ) = item

        # Each `fetch` is up to ~15s of blocking I/O. Running it in a worker
        # thread keeps the bot responsive to slash commands and messages
        # during the scrape pass.
        result = await asyncio.to_thread(self.scraper.fetch, url)

        # Transport-level failure (timeout, anti-bot block, 5xx): trust
        # nothing, change nothing. Try again next pass.
        if result.failure == FAILURE_BLOCKED:
            return
        # Page reachable but literally nothing useful was parsed — same
        # outcome. Don't overwrite known-good state with empty data.
        if result.failure == FAILURE_UNSUPPORTED and not result.has_data:
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

        if result.price is not None:
            db.add_price_history(item_id, result.price)

        if price_changed or back_in_stock or decision.alert_kind:
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
                    if decision.alert_kind:
                        new_src = _effective_currency(result.currency, url)
                        msg += self._format_alert_section(decision, new_src)
                    await user.send(msg, suppress_embeds=True)
                    logger.info(
                        f"Scrape DM sent to user {user_id} for {url} "
                        f"(price_changed={price_changed}, back_in_stock={back_in_stock}, "
                        f"alert={decision.alert_kind})"
                    )
            except Exception as e:
                logger.error(f"Could not send DM to user {user_id}: {e}")

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
        self.converter.refresh()
