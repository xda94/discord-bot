"""Currency conversion and display helpers for wishlist features."""

from __future__ import annotations

import logging
from collections import Counter

import requests

import db
from wishlist.scraper import PriceScraper

logger = logging.getLogger("discord_bot")


class CurrencyConverter:
    """Convert through the EUR-relative rates stored in SQLite."""

    EXCHANGE_API = "https://open.er-api.com/v6/latest/EUR"
    SUPPORTED_DISPLAY_CURRENCIES = ("RON", "DKK", "EUR", "USD", "GBP")
    DEFAULT_DISPLAY_CURRENCY = "RON"

    def refresh(self) -> bool:
        logger.info("Starting scheduled exchange rate update task...")
        try:
            response = requests.get(self.EXCHANGE_API, timeout=10)
            response.raise_for_status()
            rates = response.json().get("rates")
            if not rates:
                logger.error("Exchange rate API returned no rates table.")
                return False
            db.set_exchange_rate("EUR", 1.0)
            for currency in self.SUPPORTED_DISPLAY_CURRENCIES:
                if currency == "EUR":
                    continue
                if currency in rates:
                    db.set_exchange_rate(currency, rates[currency])
                else:
                    logger.warning(
                        "Currency %s missing from exchange rate API response — "
                        "keeping previously stored rate.",
                        currency,
                    )
            logger.info("Exchange rates updated successfully.")
            return True
        except requests.exceptions.RequestException as exc:
            logger.error("Failed to fetch exchange rates from API: %s", exc)
        except Exception:
            logger.exception("Error in update_exchange_rates_task")
        return False

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
        return price / from_rate * to_rate

    def to_currency(self, price, source_currency, target_currency) -> float | None:
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
        base = f"{price:.2f} {currency}" if currency else str(price)
        if not currency:
            return base
        conversions = []
        for target in ("DKK", "EUR", "USD", "GBP"):
            if currency.upper() != target:
                converted = self.convert(price, currency, target)
                if converted:
                    conversions.append(f"{converted:.2f} {target}")
        return f"{base} (~" + " | ".join(conversions) + ")" if conversions else base


def effective_currency(stored_currency: str | None, url: str) -> str | None:
    return stored_currency or PriceScraper._currency_from_tld(url)


def majority_currency(url_currency_pairs) -> str:
    counts: Counter[str] = Counter()
    for url, stored in url_currency_pairs:
        currency = effective_currency(stored, url)
        if currency:
            counts[currency.upper()] += 1
    if not counts:
        return CurrencyConverter.DEFAULT_DISPLAY_CURRENCY
    return counts.most_common(1)[0][0]


_effective_currency = effective_currency
_majority_currency = majority_currency

