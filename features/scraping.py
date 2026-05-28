import io
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

import discord
import matplotlib.pyplot as plt
import requests
from bs4 import BeautifulSoup
from discord import app_commands
from discord.ext import tasks

import db

logger = logging.getLogger("discord_bot")

# Non-interactive matplotlib backend, safe inside an async bot process.
plt.switch_backend("Agg")

# `curl_cffi` mimics a real Chrome TLS+HTTP/2 fingerprint, which bypasses the
# bot-detection used by most Romanian e-commerce sites (Altex, eMag, Cel.ro).
# If it isn't installed we fall back to plain `requests` so the bot still
# starts, just with the original "blocked by Cloudflare" limitation for those
# sites. Install with `pip install curl_cffi`.
try:
    from curl_cffi import requests as impersonated_http

    _IMPERSONATION_AVAILABLE = True
except ImportError:
    impersonated_http = None
    _IMPERSONATION_AVAILABLE = False
    logger.warning(
        "curl_cffi not installed — falling back to plain requests for scraping. "
        "Bot-protected sites (Altex/eMag/etc.) will likely time out. "
        "Install with: pip install curl_cffi"
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
    # Currencies offered as `/scrape-*` display options. Must be a subset of the
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
# Price scraper
# ---------------------------------------------------------------------------


# Slash-command currency picker. Built from `CurrencyConverter`'s supported
# set so this stays in sync with the conversion code automatically.
CURRENCY_CHOICES = [
    app_commands.Choice(name=c, value=c)
    for c in CurrencyConverter.SUPPORTED_DISPLAY_CURRENCIES
]


# Possible values for ScrapeResult.failure.
FAILURE_BLOCKED = "blocked"          # transport-level: timeout, conn refused, 4xx/5xx
FAILURE_UNSUPPORTED = "unsupported"  # HTML fetched OK but no structured data


@dataclass
class ScrapeResult:
    """Outcome of a scrape attempt.

    `failure` is None on success or one of `FAILURE_BLOCKED` / `FAILURE_UNSUPPORTED`
    so the caller can render a precise error message.
    """

    price: float | None = None
    in_stock: bool = False
    title: str | None = None
    currency: str | None = None
    failure: str | None = None

    @property
    def has_data(self) -> bool:
        return self.price is not None or self.title is not None or self.currency is not None


def _domain(url: str) -> str:
    """Return the hostname of `url`, falling back to the URL itself on parse errors."""
    try:
        return urlparse(url).hostname or url
    except Exception:
        return url


class PriceScraper:
    """Fetches a product page and tries JSON-LD → meta → text-fallback for
    price, currency, stock status, and title.

    `fetch` returns a `ScrapeResult`. Inspect `result.failure` for the failure
    category (or None on success) and the data fields for whatever was
    extracted.
    """

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    # Chrome version to impersonate via curl_cffi. Bumping this occasionally
    # keeps the fingerprint fresh.
    IMPERSONATE_TARGET = "chrome124"
    # These Romanian phrases are intentional — they're matched against the
    # page HTML to detect stock status on RO e-commerce sites that don't
    # provide structured data. Do not translate; add more languages instead.
    OUT_OF_STOCK_KEYWORDS = (
        "stoc epuizat", "indisponibil", "nu este in stoc",
        "lipsa stoc", "out of stock", "momentan indisponibil",
    )
    IN_STOCK_KEYWORDS = (
        "in stoc", "în stoc", "disponibil",
        "adauga in cos", "adaugă în coș", "add to cart",
    )
    # Last-resort currency guess from the hostname's TLD. Only consulted when
    # the page returned no JSON-LD currency and no og:price:currency meta tag.
    # Keep this conservative — only TLDs that are unambiguously tied to one
    # currency belong here. Avoid `.com`, multi-currency domains, etc.
    TLD_CURRENCY_FALLBACKS = {
        "dk": "DKK",
        "ro": "RON",
    }

    def fetch(self, url: str) -> ScrapeResult:
        """Fetch `url` and try to extract price/title/currency/stock from it."""
        try:
            response = self._http_get(url)
        except Exception as e:
            # Transport-level failure: timeout, conn refused, TLS reject, etc.
            # Almost always a bot-block on Romanian e-commerce sites.
            logger.error(f"Scraping transport error for {url}: {e}")
            return ScrapeResult(failure=FAILURE_BLOCKED)

        if response.status_code != 200:
            logger.warning(f"Scraping HTTP {response.status_code} for {url}")
            return ScrapeResult(failure=FAILURE_BLOCKED)

        try:
            soup = BeautifulSoup(response.text, "html.parser")
            price = None
            title = None
            currency = None
            in_stock: bool | None = None

            price, title, currency, in_stock = self._extract_from_json_ld(
                soup, price, title, currency, in_stock
            )
            title = title or self._extract_meta_title(soup)
            price = price or self._extract_meta_price(soup)
            currency = currency or self._extract_meta_currency(soup)
            # Last-resort: if the page didn't tell us its currency, fall back to
            # the TLD-based guess (e.g. .dk → DKK, .ro → RON). Only kicks in
            # when both JSON-LD and meta tags were silent.
            currency = currency or self._currency_from_tld(url)
            if in_stock is None:
                in_stock = self._extract_meta_availability(soup)
            if in_stock is None:
                in_stock = self._extract_text_availability(response.text)
            if in_stock is None:
                in_stock = False

            result = ScrapeResult(
                price=price,
                in_stock=in_stock,
                title=title.strip() if title else None,
                currency=currency,
            )
            if not result.has_data:
                # Page returned 200 but had no JSON-LD, no meta tags, and no
                # text signals. Most likely a JS-rendered SPA.
                result.failure = FAILURE_UNSUPPORTED
            return result
        except Exception as e:
            logger.error(f"Scraping parse error for {url}: {e}")
            return ScrapeResult(failure=FAILURE_UNSUPPORTED)

    def _http_get(self, url: str):
        """Perform the HTTP GET. Prefer curl_cffi's Chrome impersonation so we
        can pass bot-detection on protected sites; fall back to plain requests
        when curl_cffi is unavailable."""
        if _IMPERSONATION_AVAILABLE:
            return impersonated_http.get(
                url, impersonate=self.IMPERSONATE_TARGET, timeout=15
            )
        return requests.get(url, headers={"User-Agent": self.USER_AGENT}, timeout=15)

    @staticmethod
    def _extract_from_json_ld(soup, price, title, currency, in_stock):
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if not (isinstance(item, dict) and
                            item.get("@type") in ("Product", "http://schema.org/Product")):
                        continue
                    title = title or item.get("name")
                    offers = item.get("offers")
                    if isinstance(offers, dict):
                        offer_list = [offers]
                    elif isinstance(offers, list):
                        offer_list = offers
                    else:
                        offer_list = []
                    for offer in offer_list:
                        if not isinstance(offer, dict):
                            continue
                        # Price may sit directly on the offer or inside a nested
                        # PriceSpecification object (Schema.org's more formal
                        # form used by Altex, eMag, and others).
                        spec = offer.get("priceSpecification") or {}
                        if isinstance(spec, list):
                            spec = spec[0] if spec else {}
                        raw_price = offer.get("price")
                        if raw_price is None and isinstance(spec, dict):
                            raw_price = spec.get("price")
                        if price is None and raw_price is not None:
                            try:
                                price = float(str(raw_price).replace(",", "."))
                            except ValueError:
                                pass
                        if currency is None:
                            currency = offer.get("priceCurrency")
                            if currency is None and isinstance(spec, dict):
                                currency = spec.get("priceCurrency")
                        availability = offer.get("availability", "")
                        if availability:
                            in_stock = "InStock" in availability or "PreOrder" in availability
            except Exception:
                continue
        return price, title, currency, in_stock

    @staticmethod
    def _extract_meta_title(soup):
        title_tag = soup.find("meta", property="og:title") or soup.find("title")
        if not title_tag:
            return None
        if title_tag.has_attr("content"):
            return title_tag["content"]
        return title_tag.string

    @staticmethod
    def _extract_meta_price(soup):
        tag = soup.find("meta", property="product:price:amount") or soup.find(
            "meta", property="og:price:amount"
        )
        if not tag:
            return None
        try:
            return float(tag["content"].replace(",", "."))
        except Exception:
            return None

    @staticmethod
    def _extract_meta_currency(soup):
        tag = soup.find("meta", property="product:price:currency") or soup.find(
            "meta", property="og:price:currency"
        )
        return tag["content"] if tag else None

    @classmethod
    def _currency_from_tld(cls, url: str) -> str | None:
        """Guess a currency from the URL's hostname TLD using
        `TLD_CURRENCY_FALLBACKS`. Returns None when the TLD isn't mapped or
        the URL can't be parsed."""
        try:
            host = urlparse(url).hostname or ""
        except Exception:
            return None
        if not host:
            return None
        tld = host.rsplit(".", 1)[-1].lower()
        guess = cls.TLD_CURRENCY_FALLBACKS.get(tld)
        if guess:
            logger.debug(f"TLD fallback currency {guess} for {host}")
        return guess

    @staticmethod
    def _extract_meta_availability(soup):
        tag = soup.find("meta", property="product:availability") or soup.find(
            "meta", property="og:availability"
        )
        if not tag:
            return None
        content = tag.get("content", "").lower()
        return "instock" in content or "in stoc" in content

    @classmethod
    def _extract_text_availability(cls, html_text: str):
        text_lower = html_text.lower()
        # Check negative keywords first so "out of stock" doesn't get overridden
        # by a generic "in stoc" sitting elsewhere on the page.
        if any(k in text_lower for k in cls.OUT_OF_STOCK_KEYWORDS):
            return False
        if any(k in text_lower for k in cls.IN_STOCK_KEYWORDS):
            return True
        return None


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

        @self.tree.command(name="scrape-item", description="Add a link to track price and stock")
        @app_commands.describe(url="The URL of the item to track")
        async def scrape_item(interaction: discord.Interaction, url: str):
            logger.info(f"Command /scrape-item called by {interaction.user} for {url}")
            await interaction.response.defer(ephemeral=True)

            result = feature.scraper.fetch(url)

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
            display_name = f"**{result.title}**" if result.title else f"**{url}**"
            await interaction.followup.send(
                f"✅ Added: {display_name}\n"
                f"Current price: `{price_str}` | In stock: `{'Yes' if result.in_stock else 'No'}`",
                ephemeral=True,
                suppress_embeds=True,
            )

        @self.tree.command(name="scrape-item-delete", description="Remove a link from tracking")
        @app_commands.describe(url="The URL to remove")
        async def scrape_item_delete(interaction: discord.Interaction, url: str):
            logger.info(f"Command /scrape-item-delete called by {interaction.user} for {url}")
            if db.delete_scraped_item(interaction.user.id, url):
                await interaction.response.send_message("Link removed and data cleared.", ephemeral=True)
            else:
                await interaction.response.send_message("Link not found in your list.", ephemeral=True)

        @self.tree.command(
            name="scrape-show", description="Show your tracked items and their current prices"
        )
        @app_commands.describe(
            currency=f"Display currency (default: {CurrencyConverter.DEFAULT_DISPLAY_CURRENCY})"
        )
        @app_commands.choices(currency=CURRENCY_CHOICES)
        async def scrape_show(
            interaction: discord.Interaction,
            currency: app_commands.Choice[str] | None = None,
        ):
            target_currency = (
                currency.value if currency else CurrencyConverter.DEFAULT_DISPLAY_CURRENCY
            )
            logger.info(
                f"Command /scrape-show called by {interaction.user} (currency={target_currency})"
            )
            await interaction.response.defer(ephemeral=True)

            items = db.get_user_scraped_items(interaction.user.id)
            if not items:
                await interaction.followup.send("You are not tracking any items.", ephemeral=True)
                return

            blocks = []
            for url, price, stock, title, item_currency in items:
                status = "✅ In stock" if stock else "❌ Out of stock"
                price_display = (
                    f"`{feature.converter.format_in_currency(price, item_currency, target_currency)}`"
                    if price is not None else "N/A"
                )
                item_name = f"**{title}**" if title else f"🔗 {url}"
                blocks.append(f"{item_name}\nURL: {url}\n💰 Price: {price_display} | {status}")

            # Group items into chunks under Discord's 2000-char message limit.
            chunks = []
            current = f"**Your tracked items** (prices in **{target_currency}**):\n\n"
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
            name="scrape-graph", description="Generate a price history graph for a tracked item"
        )
        @app_commands.describe(
            url="The URL of the item",
            currency=f"Display currency (default: {CurrencyConverter.DEFAULT_DISPLAY_CURRENCY})",
        )
        @app_commands.choices(currency=CURRENCY_CHOICES)
        async def scrape_graph(
            interaction: discord.Interaction,
            url: str,
            currency: app_commands.Choice[str] | None = None,
        ):
            target_currency = (
                currency.value if currency else CurrencyConverter.DEFAULT_DISPLAY_CURRENCY
            )
            logger.info(
                f"Command /scrape-graph called by {interaction.user} for {url} "
                f"(currency={target_currency})"
            )
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
            item_currency = (
                item_info[4] if item_info and len(item_info) > 4 else None
            )

            # Convert each point to the requested display currency. If a point
            # can't be converted we drop it — better an honest gap than a misleading
            # number labelled in the wrong unit.
            timestamps: list[str] = []
            prices: list[float] = []
            for raw_price, ts, _ in history:
                converted = feature.converter.to_currency(
                    raw_price, item_currency, target_currency
                )
                if converted is None:
                    continue
                timestamps.append(datetime.fromtimestamp(ts).strftime("%d/%m %H:%M"))
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
            name="scrape-graph-all",
            description="Combined price history graph for ALL your tracked items",
        )
        @app_commands.describe(
            currency=f"Display currency (default: {CurrencyConverter.DEFAULT_DISPLAY_CURRENCY})"
        )
        @app_commands.choices(currency=CURRENCY_CHOICES)
        async def scrape_graph_all(
            interaction: discord.Interaction,
            currency: app_commands.Choice[str] | None = None,
        ):
            target_currency = (
                currency.value if currency else CurrencyConverter.DEFAULT_DISPLAY_CURRENCY
            )
            logger.info(
                f"Command /scrape-graph-all called by {interaction.user} "
                f"(currency={target_currency})"
            )
            await interaction.response.defer(ephemeral=True)

            items = db.get_user_scraped_items(interaction.user.id)
            if not items:
                await interaction.followup.send(
                    "You are not tracking any items.", ephemeral=True
                )
                return

            # Build one (label, [(datetime, price_in_target), ...]) series per item.
            # All series share a single Y-axis in `target_currency` so
            # cross-currency comparisons are valid.
            series: list[tuple[str, list[tuple[datetime, float]]]] = []
            skipped_no_history = 0
            skipped_no_currency = 0

            for url, _last_price, _stock, title, item_currency in items:
                history = db.get_price_history(interaction.user.id, url)
                if not history:
                    skipped_no_history += 1
                    continue

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

    @staticmethod
    def _render_price_graph(timestamps, prices, title, item_currency) -> discord.File:
        fig, ax = plt.subplots(figsize=(10, 6), facecolor="#2f3136")
        ax.set_facecolor("#36393f")
        ax.plot(timestamps, prices, marker="o", linestyle="-", color="#7289da", linewidth=2)

        ax.set_title(f"Price Evolution: {title[:50]}", color="white", fontsize=14)
        ax.set_xlabel("Date & Time", color="white")
        ax.set_ylabel(f"Price ({item_currency})", color="white")
        ax.tick_params(axis="x", rotation=45, colors="white")
        ax.tick_params(axis="y", colors="white")
        for spine in ax.spines.values():
            spine.set_color("white")
        ax.grid(True, color="#4f545c", linestyle="--", linewidth=0.5)
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

        # Auto-format the date axis (rotation, tick density) based on the range.
        fig.autofmt_xdate()
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return discord.File(buf, filename="price_history_all.png")

    @tasks.loop(hours=12)
    async def _scrape_loop(self):
        logger.info("Starting scheduled price scrape task...")
        items = db.get_all_scraped_items()

        for item_id, user_id, url, old_price, old_stock_status, old_title, old_currency in items:
            result = self.scraper.fetch(url)
            # Skip transient failures so we don't overwrite good data with None.
            if result.price is None:
                continue

            db.add_price_history(item_id, result.price)

            price_changed = old_price is not None and result.price != old_price
            back_in_stock = not old_stock_status and result.in_stock

            if price_changed or back_in_stock:
                try:
                    user = await self.client.fetch_user(user_id)
                    if user:
                        disp_name = result.title or old_title or url
                        msg = f"🔔 **Update: {disp_name}**\nLink: {url}\n"
                        if back_in_stock:
                            msg += "✅ Item is now **BACK IN STOCK**!\n"
                        if price_changed:
                            old_str = self.converter.format_with_conversions(old_price, old_currency)
                            new_str = self.converter.format_with_conversions(result.price, result.currency)
                            msg += f"💰 Price changed: `{old_str}` -> **{new_str}**\n"
                        await user.send(msg, suppress_embeds=True)
                        logger.info(f"Price alert sent to user {user_id} for {url}")
                except Exception as e:
                    logger.error(f"Could not send DM to user {user_id}: {e}")

            db.update_scraped_item_status(
                item_id, result.price, result.in_stock, result.title, result.currency,
            )

        db.clean_old_price_history(days=5)
        logger.info("Finished price scrape task and cleaned history.")

    @tasks.loop(hours=24)
    async def _refresh_rates_loop(self):
        self.converter.refresh()
