from __future__ import annotations
"""Pure web-scraping utilities — no Discord or chart-renderer dependencies.

Part of the shared `wishlist` package so that:

  - the Flask API can validate a URL synchronously at POST time without
    pulling discord.py + chart rendering into the API process, where neither
    is used.
  - the bot and API share exactly the same extraction behavior.

This module is intentionally minimal in its imports: only the things needed
to fetch a page, parse HTML, and decide whether the result is useful.
"""

import json
import logging
import math
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from llm.client import query_llm
from llm.capacity import CapacityError

# Distinct logger name so the bot's "discord_bot" file handler and the API's
# "flask_api" file handler can both pick this up via the setup wired in
# `logger.py`. Without that attachment these messages go nowhere when the
# module is imported from either process.
logger = logging.getLogger("scraper")


# ---------------------------------------------------------------------------
# Optional curl_cffi for TLS fingerprint impersonation
# ---------------------------------------------------------------------------
#
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
# Result type
# ---------------------------------------------------------------------------

# Possible values for ScrapeResult.failure.
FAILURE_BLOCKED = "blocked"          # transport-level: timeout, conn refused, 4xx/5xx
FAILURE_UNSUPPORTED = "unsupported"  # HTML fetched OK but no structured data
FAILURE_BUSY = "busy"


def _normalize_price(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        price = float(str(value).replace(",", "."))
    except (TypeError, ValueError, OverflowError):
        return None
    return price if math.isfinite(price) and price >= 0 else None


def _normalize_text(value):
    if isinstance(value, str):
        return value.strip() or None
    return None


def _availability(value):
    value = _normalize_text(value)
    if value is None:
        return None
    value = value.rstrip("/").rsplit("/", 1)[-1].lower()
    if value in {"instock", "preorder", "in stock", "in stoc", "în stoc"}:
        return True
    if value in {"outofstock", "soldout", "discontinued", "out of stock", "stoc epuizat", "indisponibil"}:
        return False
    return None


@dataclass
class ScrapeResult:
    """Outcome of a scrape attempt.

    `in_stock` is tri-state: True / False / None. `None` means "couldn't
    determine" — distinct from False ("definitely out of stock"). The
    bot's scrape loop relies on this distinction to avoid mis-firing the
    "back in stock" DM on a flapping signal, and to leave the persisted
    `last_stock_status` untouched on unknown reads.
    """

    price: float | None = None
    in_stock: bool | None = None
    title: str | None = None
    currency: str | None = None
    failure: str | None = None

    def __post_init__(self):
        self.price = _normalize_price(self.price)
        self.title = _normalize_text(self.title)
        self.currency = _normalize_text(self.currency)
        if not isinstance(self.in_stock, bool):
            self.in_stock = None

    @property
    def has_data(self) -> bool:
        # `in_stock` counts here too — a successful text-fallback stock read
        # is a real signal even when price/title/currency are missing.
        return _normalize_price(self.price) is not None or isinstance(self.in_stock, bool)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def _domain(url: str) -> str:
    """Return the hostname of `url`, falling back to the URL itself on parse errors."""
    try:
        return urlparse(url).hostname or url
    except Exception:
        return url


def _is_valid_http_url(url: str) -> bool:
    """Cheap sanity check before we spend a network round-trip on a URL.

    Rejects:
      - non-strings / empty input
      - non-`http(s)` schemes (incl. `javascript:`, `file:`, `data:`, …)
      - URLs with no hostname (`http://`, `https:///foo`)
      - anything `urlparse` can't make sense of at all
    """
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


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
    # Keep this conservative — only TLDs that are unambiguously tied to one
    # currency belong here. Avoid `.com`, multi-currency domains, etc.
    TLD_CURRENCY_FALLBACKS = {
        "dk": "DKK",
        "ro": "RON",
    }

    def fetch(self, url: str, *, capacity_policy="interactive") -> ScrapeResult:
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
            if price is None:
                price = self._extract_meta_price(soup)
            currency = currency or self._extract_meta_currency(soup)
            if in_stock is None:
                in_stock = self._extract_meta_availability(soup)
            
            # Strip out script and style elements before text fallback / LLM extraction
            # This avoids falsely matching out-of-stock translation keys in JSON bundles.
            for element in soup(["script", "style"]):
                element.decompose()
            clean_text = soup.get_text(separator=' ', strip=True)

            if in_stock is None:
                in_stock = self._extract_text_availability(clean_text)
            # NOTE: deliberately do NOT coerce `in_stock=None` to False here.
            # Unknown stock status is propagated up so the scrape loop can
            # leave the persisted value alone (via COALESCE) instead of
            # mis-stamping the item as out-of-stock.

            if price is None and in_stock is None:
                # Fall back to LLM extraction if standard methods failed
                # Extract clean text and truncate to avoid huge context windows
                llm_price, llm_title, llm_currency, llm_stock = self._extract_with_llm(
                    clean_text, capacity_policy=capacity_policy
                )
                
                price = llm_price if llm_price is not None else price
                title = title if title is not None else llm_title
                currency = currency if currency is not None else llm_currency
                in_stock = llm_stock if llm_stock is not None else in_stock

            currency = currency or self._currency_from_tld(url)

            result = ScrapeResult(
                price=price,
                in_stock=in_stock,
                title=title,
                currency=currency,
            )
            if not result.has_data:
                result.failure = FAILURE_UNSUPPORTED
            return result
        except CapacityError:
            return ScrapeResult(failure=FAILURE_BUSY)
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
                    title = title or _normalize_text(item.get("name"))
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
                        if price is None:
                            price = _normalize_price(raw_price)
                        if currency is None:
                            currency = _normalize_text(offer.get("priceCurrency"))
                            if currency is None and isinstance(spec, dict):
                                currency = _normalize_text(spec.get("priceCurrency"))
                        if in_stock is None:
                            in_stock = _availability(offer.get("availability"))
            except Exception:
                continue
        return price, title, currency, in_stock

    @staticmethod
    def _extract_meta_title(soup):
        title_tag = soup.find("meta", property="og:title") or soup.find("title")
        if not title_tag:
            return None
        if title_tag.has_attr("content"):
            return _normalize_text(title_tag["content"])
        return _normalize_text(title_tag.string)

    @staticmethod
    def _extract_meta_price(soup):
        tag = soup.find("meta", property="product:price:amount") or soup.find(
            "meta", property="og:price:amount"
        )
        if not tag:
            return None
        return _normalize_price(tag.get("content"))

    @staticmethod
    def _extract_meta_currency(soup):
        tag = soup.find("meta", property="product:price:currency") or soup.find(
            "meta", property="og:price:currency"
        )
        return _normalize_text(tag.get("content")) if tag else None

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
        return _availability(tag.get("content"))

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

    @staticmethod
    def _extract_with_llm(text: str, *, capacity_policy="interactive") -> tuple[float | None, str | None, str | None, bool | None]:
        # Truncate text to roughly 3000 words to save context
        words = text.split()
        if len(words) > 3000:
            text = " ".join(words[:3000])

        prompt = (
            "You are a web scraping assistant. Extract the product information from the following webpage text. "
            "Return ONLY a valid JSON object with these exact keys:\n"
            "- \"title\": string (the name of the product), or null if not found\n"
            "- \"price\": number (the price as a float), or null if not found\n"
            "- \"currency\": string (the 3-letter currency code, e.g., 'RON', 'EUR', 'USD'), or null if not found\n"
            "- \"in_stock\": boolean (true if available/in stock, false if out of stock), or null if unknown\n\n"
            f"Webpage text:\n{text}"
        )
        try:
            response = query_llm(
                prompt, options={"format": "json", "temperature": 0.0},
                capacity_policy=capacity_policy,
            )
            data = json.loads(response)
            
            price = _normalize_price(data.get("price"))
                    
            title = _normalize_text(data.get("title"))
                
            currency = _normalize_text(data.get("currency"))
            if currency is not None and len(currency) > 5:
                currency = None
                
            in_stock = data.get("in_stock")
            if not isinstance(in_stock, bool):
                in_stock = None
                
            return price, title, currency, in_stock
        except CapacityError:
            raise
        except Exception as e:
            logger.warning(f"LLM fallback extraction failed: {e}")
            return None, None, None, None
