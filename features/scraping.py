import io
import json
import logging
from datetime import datetime

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
    TARGET_CURRENCIES = ("RON", "USD", "GBP")

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


class PriceScraper:
    """Fetches a product page and tries JSON-LD → meta → text-fallback for
    price, currency, stock status, and title.

    `fetch` returns a 4-tuple `(price, in_stock, title, currency)`. When all of
    `price`, `title`, and `currency` are None the caller should treat the page
    as "unusable" — either the request was blocked at the transport layer or
    none of the three extraction strategies (JSON-LD, meta, text) matched.
    """

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    # Chrome version to impersonate via curl_cffi. Bumping this occasionally
    # keeps the fingerprint fresh.
    IMPERSONATE_TARGET = "chrome124"
    OUT_OF_STOCK_KEYWORDS = (
        "stoc epuizat", "indisponibil", "nu este in stoc",
        "lipsa stoc", "out of stock", "momentan indisponibil",
    )
    IN_STOCK_KEYWORDS = (
        "in stoc", "în stoc", "disponibil",
        "adauga in cos", "adaugă în coș", "add to cart",
    )

    def fetch(self, url: str):
        """Return (price, in_stock, title, currency). Any field may be None on
        failure; in_stock defaults to False if no signal is found."""
        try:
            response = self._http_get(url)
            if response.status_code != 200:
                return None, False, None, None

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
            if in_stock is None:
                in_stock = self._extract_meta_availability(soup)
            if in_stock is None:
                in_stock = self._extract_text_availability(response.text)
            if in_stock is None:
                in_stock = False

            return price, in_stock, title.strip() if title else None, currency
        except Exception as e:
            logger.error(f"Scraping error for {url}: {e}")
            return None, False, None, None

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

            price, stock, title, currency = feature.scraper.fetch(url)

            # Refuse to insert dead entries. If the scrape returned nothing
            # extractable at all, the URL is either anti-bot-blocked or the
            # page format is unsupported — adding it would create a permanent
            # "N/A" row that never recovers.
            if price is None and title is None and currency is None:
                await interaction.followup.send(
                    "❌ Nu am putut extrage informații de pe acest link.\n"
                    "Cauze posibile: site-ul blochează scraperul (anti-bot), pagina necesită "
                    "JavaScript, sau formatul ei nu este suportat. Linkul **nu** a fost adăugat.",
                    ephemeral=True,
                    suppress_embeds=True,
                )
                return

            item_id = db.add_scraped_item(interaction.user.id, url, title, price, stock, currency)
            if not item_id:
                await interaction.followup.send(
                    "Acest link este deja în lista ta de monitorizare.", ephemeral=True
                )
                return

            if price is not None:
                db.add_price_history(item_id, price)
            price_str = feature.converter.format_with_conversions(price, currency)
            display_name = f"**{title}**" if title else f"**{url}**"
            await interaction.followup.send(
                f"✅ Adăugat: {display_name}\nPreț actual: `{price_str}` | Stoc: `{'Da' if stock else 'Nu'}`",
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
        async def scrape_show(interaction: discord.Interaction):
            logger.info(f"Command /scrape-show called by {interaction.user}")
            await interaction.response.defer(ephemeral=True)

            items = db.get_user_scraped_items(interaction.user.id)
            if not items:
                await interaction.followup.send("You are not tracking any items.", ephemeral=True)
                return

            blocks = []
            for url, price, stock, title, currency in items:
                status = "✅ In stock" if stock else "❌ Out of stock"
                price_display = (
                    f"`{feature.converter.format_with_conversions(price, currency)}`"
                    if price is not None else "N/A"
                )
                item_name = f"**{title}**" if title else f"🔗 {url}"
                blocks.append(f"{item_name}\nURL: {url}\n💰 Price: {price_display} | {status}")

            # Group items into chunks under Discord's 2000-char message limit.
            chunks = []
            current = "**Your tracked items:**\n\n"
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
        @app_commands.describe(url="The URL of the item")
        async def scrape_graph(interaction: discord.Interaction, url: str):
            logger.info(f"Command /scrape-graph called by {interaction.user} for {url}")
            await interaction.response.defer(ephemeral=True)

            history = db.get_price_history(interaction.user.id, url)
            if not history:
                await interaction.followup.send(
                    "No price history found for this URL in your list.", ephemeral=True
                )
                return

            prices = [h[0] for h in history]
            timestamps = [datetime.fromtimestamp(h[1]).strftime("%d/%m %H:%M") for h in history]
            title = history[0][2] or "Price History"

            item_info = next(
                (item for item in db.get_user_scraped_items(interaction.user.id) if item[0] == url),
                None,
            )
            item_currency = (
                item_info[4] if item_info and len(item_info) > 4 else "N/A"
            )

            file = feature._render_price_graph(timestamps, prices, title, item_currency)
            await interaction.followup.send(file=file, ephemeral=True)

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

    @tasks.loop(hours=12)
    async def _scrape_loop(self):
        logger.info("Starting scheduled price scrape task...")
        items = db.get_all_scraped_items()

        for item_id, user_id, url, old_price, old_stock_status, old_title, old_currency in items:
            new_price, is_in_stock, new_title, new_currency = self.scraper.fetch(url)
            if new_price is None:
                continue

            db.add_price_history(item_id, new_price)

            price_changed = old_price is not None and new_price != old_price
            back_in_stock = not old_stock_status and is_in_stock

            if price_changed or back_in_stock:
                try:
                    user = await self.client.fetch_user(user_id)
                    if user:
                        disp_name = new_title or old_title or url
                        msg = f"🔔 **Update: {disp_name}**\nLink: {url}\n"
                        if back_in_stock:
                            msg += "✅ Item is now **BACK IN STOCK**!\n"
                        if price_changed:
                            old_str = self.converter.format_with_conversions(old_price, old_currency)
                            new_str = self.converter.format_with_conversions(new_price, new_currency)
                            msg += f"💰 Price changed: `{old_str}` -> **{new_str}**\n"
                        await user.send(msg, suppress_embeds=True)
                        logger.info(f"Price alert sent to user {user_id} for {url}")
                except Exception as e:
                    logger.error(f"Could not send DM to user {user_id}: {e}")

            db.update_scraped_item_status(item_id, new_price, is_in_stock, new_title, new_currency)

        db.clean_old_price_history(days=5)
        logger.info("Finished price scrape task and cleaned history.")

    @tasks.loop(hours=24)
    async def _refresh_rates_loop(self):
        self.converter.refresh()
