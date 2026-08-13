from __future__ import annotations
"""Pure web-scraping utilities — no Discord or Matplotlib dependencies.

Split out of `features/scraping.py` so that:

  - `api.py` (Flask) can validate a URL synchronously at POST time without
    pulling discord.py + matplotlib into the API process (those drag ~100 MB
    of RSS on a Pi Zero W and are completely unused on the API side).
  - The bot side still gets the same `PriceScraper` via re-exports in
    `features/scraping.py`, so nothing else has to change.

This module is intentionally minimal in its imports: only the things needed
to fetch a page, parse HTML, and decide whether the result is useful.
"""

import json
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from ollama_client import query_ollama

# Distinct logger name so the bot's "discord_bot" file handler and the API's
# "flask_api" file handler can both pick this up via the setup wired in
# `logger.py`. Without that attachment these messages go nowhere when the
# module is imported from `api.py`.
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


@dataclass
class ScrapeResult:
    """Outcome of a scrape attempt.

    `failure` is None on success or one of `FAILURE_BLOCKED` / `FAILURE_UNSUPPORTED`
    so the caller can render a precise error message.

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

    @property
    def has_data(self) -> bool:
        # `in_stock` counts here too — a successful text-fallback stock read
        # is a real signal even when price/title/currency are missing.
        return (
            self.price is not None
            or self.title is not None
            or self.currency is not None
            or self.in_stock is not None
        )


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

            if price is None and title is None and currency is None and in_stock is None:
                # Fall back to LLM extraction if standard methods failed
                # Extract clean text and truncate to avoid huge context windows
                llm_price, llm_title, llm_currency, llm_stock = self._extract_with_llm(clean_text)
                
                price = llm_price if llm_price is not None else price
                title = llm_title if llm_title is not None else title
                currency = llm_currency if llm_currency is not None else currency
                in_stock = llm_stock if llm_stock is not None else in_stock

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

    @staticmethod
    def _extract_with_llm(text: str) -> tuple[float | None, str | None, str | None, bool | None]:
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
            response = query_ollama(prompt, options={"format": "json", "temperature": 0.0})
            data = json.loads(response)
            
            price = data.get("price")
            if price is not None:
                try:
                    price = float(price)
                except (ValueError, TypeError):
                    price = None
                    
            title = data.get("title")
            if not isinstance(title, str):
                title = None
                
            currency = data.get("currency")
            if not isinstance(currency, str):
                currency = None
            elif len(currency) > 5:
                currency = None
                
            in_stock = data.get("in_stock")
            if not isinstance(in_stock, bool):
                in_stock = None
                
            return price, title, currency, in_stock
        except Exception as e:
            logger.warning(f"LLM fallback extraction failed: {e}")
            return None, None, None, None
