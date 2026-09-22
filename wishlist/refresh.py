"""Shared fetch-and-persist operation for manual wishlist refreshes."""

from __future__ import annotations

import db
from wishlist.scraper import FAILURE_BLOCKED, FAILURE_UNSUPPORTED, PriceScraper, ScrapeResult


def refresh_item(item, scraper: PriceScraper) -> ScrapeResult:
    """Fetch an item and persist usable data using existing refresh semantics."""
    item_id, _user_id, url, old_price, *_rest = item
    result = scraper.fetch(url)
    status = result.failure or "ok"
    db.update_scraped_item_check_status(item_id, status)
    if result.failure == FAILURE_BLOCKED:
        return result
    if result.failure == FAILURE_UNSUPPORTED and not result.has_data:
        return result
    if result.price is not None and result.price != old_price:
        db.add_price_history(item_id, result.price)
    db.update_scraped_item_status(
        item_id, result.price, result.in_stock, result.title, result.currency
    )
    return result

