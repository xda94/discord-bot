"""Shared fetch-and-persist operation for manual wishlist refreshes."""

from __future__ import annotations

import db
from db.wishlist import persist_scraped_item_refresh
from wishlist.scraper import FAILURE_BLOCKED, FAILURE_BUSY, FAILURE_DATABASE, FAILURE_UNSUPPORTED, PriceScraper, ScrapeResult


def refresh_item(item, scraper: PriceScraper) -> ScrapeResult:
    """Fetch an item and persist usable data using existing refresh semantics."""
    item_id, _user_id, url, old_price, *_rest = item
    result = scraper.fetch(url)
    status = result.failure or "ok"
    if result.failure in (FAILURE_BLOCKED, FAILURE_BUSY) or (result.failure == FAILURE_UNSUPPORTED and not result.has_data):
        if not db.update_scraped_item_check_status(item_id, status):
            result.failure = FAILURE_DATABASE
        return result
    if not persist_scraped_item_refresh(item_id, result.price, result.in_stock, result.title, result.currency, status, record_history=True, changed_only=True):
        result.failure = FAILURE_DATABASE
    return result
