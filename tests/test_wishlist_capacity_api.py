import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import discord
import pytest
from discord import app_commands

import db
from features.natural_commands import NaturalCommandsFeature
from features.wishlist import WishlistFeature
from llm.capacity import CapacityBusy
from web.app import create_app
from web.routes import wishlist as routes
from wishlist.scraper import FAILURE_BUSY, PriceScraper


@pytest.fixture
def client(tmp_db):
    client = create_app({"TESTING": True, "API_TOKEN": "test-token"}).test_client()
    client.environ_base["HTTP_AUTHORIZATION"] = "Bearer test-token"
    return client


@pytest.fixture
def scraper(monkeypatch):
    scraper = PriceScraper()
    monkeypatch.setattr(scraper, "_http_get", lambda _url: SimpleNamespace(
        status_code=200, text="<title>Source title</title>"
    ))
    return scraper


@pytest.mark.parametrize("endpoint", ["add", "refresh"])
def test_wishlist_capacity_failure_is_temporary_and_preserves_data(client, scraper, monkeypatch, endpoint):
    url = "https://shop.example.ro/item"
    if endpoint == "refresh":
        item_id = db.add_scraped_item(7, url, title="Saved", price=100, stock=True, currency="RON")
        db.add_price_history(item_id, 100)
    original = db.get_scraped_item(7, url)
    history = db.get_price_history(7, url)
    monkeypatch.setattr(routes, "_price_scraper", scraper)
    query = Mock(side_effect=CapacityBusy("Busy"))
    monkeypatch.setattr("wishlist.scraper.query_llm", query)

    response = client.post(f"/wishlist/{endpoint}", json={"user_id": 7, "url": url})

    assert response.status_code == 503
    assert response.get_json()["error"] == FAILURE_BUSY
    assert "try again" in response.get_json()["detail"]
    assert query.call_args.kwargs["capacity_policy"] == "interactive"
    if original is None:
        assert db.get_scraped_item(7, url) is None
    else:
        refreshed = db.get_scraped_item(7, url)
        assert refreshed[3:13] == original[3:13]
        assert refreshed[14] == FAILURE_BUSY
    assert db.get_price_history(7, url) == history


def test_wishlist_add_metadata_only_fallback_failure_is_unsupported(client, scraper, monkeypatch):
    monkeypatch.setattr(routes, "_price_scraper", scraper)
    query = Mock(return_value='{"title":"Site","currency":"RON"}')
    monkeypatch.setattr("wishlist.scraper.query_llm", query)
    url = "https://shop.example.ro/item"

    response = client.post("/wishlist/add", json={"user_id": 7, "url": url})

    query.assert_called_once()
    assert response.status_code == 422
    assert response.get_json()["error"] == "unsupported"
    assert db.get_scraped_item(7, url) is None


def test_wishlist_add_fallback_zero_and_false_are_saved(client, scraper, monkeypatch):
    monkeypatch.setattr(routes, "_price_scraper", scraper)
    monkeypatch.setattr("wishlist.scraper.query_llm", Mock(return_value='{"price":0,"currency":"EUR","in_stock":false}'))
    url = "https://shop.example.ro/item"

    response = client.post("/wishlist/add", json={"user_id": 7, "url": url})

    assert response.status_code == 201
    assert response.get_json()["in_stock"] is False
    assert db.get_scraped_item(7, url)[3:7] == (0, 0, "Source title", "EUR")
    assert [row[0] for row in db.get_price_history(7, url)] == [0]


def test_discord_add_reports_busy_without_saving(tmp_db, scraper, monkeypatch):
    bot = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(bot)
    feature = WishlistFeature(bot, tree)
    feature.scraper = scraper
    monkeypatch.setattr("wishlist.scraper.query_llm", Mock(side_effect=CapacityBusy("Busy")))
    interaction = MagicMock()
    interaction.user.id = 7
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    url = "https://shop.example.ro/item"

    asyncio.run(tree.get_command("wishlist-item").callback(interaction, url))

    text = interaction.followup.send.await_args.args[0]
    assert "temporarily busy" in text
    assert "try again" in text
    assert db.get_scraped_item(7, url) is None


def test_discord_manual_refresh_busy_retains_snapshot(tmp_db, scraper, monkeypatch):
    url = "https://shop.example.ro/item"
    item_id = db.add_scraped_item(7, url, title="Saved", price=100, stock=False, currency="RON")
    db.add_price_history(item_id, 100)
    item = db.get_scraped_item(7, url)
    history = db.get_price_history(7, url)
    feature = object.__new__(WishlistFeature)
    feature.scraper = scraper
    query = Mock(side_effect=CapacityBusy("Busy"))
    monkeypatch.setattr("wishlist.scraper.query_llm", query)

    text = asyncio.run(feature._manual_refresh_item(item))

    assert "temporarily busy" in text
    assert "try again" in text
    assert query.call_args.kwargs["capacity_policy"] == "interactive"
    assert db.get_scraped_item(7, url)[3:13] == item[3:13]
    assert db.get_price_history(7, url) == history


def test_scheduled_scrape_background_busy_preserves_snapshot_and_alerts(tmp_db, scraper, monkeypatch):
    url = "https://shop.example.ro/item"
    item_id = db.add_scraped_item(7, url, title="Saved", price=100, stock=False, currency="RON")
    db.add_price_history(item_id, 100)
    db.update_item_alert_state(item_id, "low", 100)
    item = db.get_scraped_item(7, url)
    history = db.get_price_history(7, url)
    feature = object.__new__(WishlistFeature)
    feature.scraper = scraper
    query = Mock(side_effect=CapacityBusy("Busy"))
    monkeypatch.setattr("wishlist.scraper.query_llm", query)
    delivery = AsyncMock()
    monkeypatch.setattr("features.wishlist.deliver_tracking", delivery)

    asyncio.run(feature._process_scrape_item(item))

    assert query.call_args.kwargs["capacity_policy"] == "background"
    assert db.get_scraped_item(7, url)[3:13] == item[3:13]
    assert db.get_scraped_item(7, url)[14] == FAILURE_BUSY
    assert db.get_price_history(7, url) == history
    delivery.assert_not_awaited()


def test_natural_wishlist_add_explains_temporary_busy(tmp_db, monkeypatch):
    message = SimpleNamespace(
        author=SimpleNamespace(id=7, send=AsyncMock()),
        channel=SimpleNamespace(id=20), reference=None,
        reply=AsyncMock(), add_reaction=AsyncMock(),
    )
    monkeypatch.setattr("features.natural_commands.extract_mention_text", lambda *_: "track https://shop.example/item")
    wishlist = SimpleNamespace(add_item_for_user=AsyncMock(return_value=SimpleNamespace(status=FAILURE_BUSY)))
    feature = NaturalCommandsFeature(None, bot_id=99, wishlist=wishlist, flights=None, reminders=None)

    assert asyncio.run(feature.handle_message(message)) is True

    assert "temporarily busy" in message.reply.await_args.args[0]
    assert "try again" in message.reply.await_args.args[0]
    message.add_reaction.assert_not_awaited()
