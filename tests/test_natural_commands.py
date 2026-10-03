import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from features.natural_commands import NaturalCommandsFeature
from features.wishlist import WishlistFeature
from natural_commands import (
    NaturalCommandError,
    ClearWishlistTargetAction,
    DeleteWishlistAction,
    ReminderAction,
    RefreshWishlistAction,
    ShowFlightsAction,
    ShowWishlistAction,
    TrackURLAction,
    WishlistGraphAction,
    WishlistRestockAction,
    WishlistTargetAction,
    parse_natural_command,
)


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("track https://example.com/item", TrackURLAction),
        ("urmărește https://example.com/item", TrackURLAction),
        ("show my flights", ShowFlightsAction),
        ("arată-mi zborurile mele", ShowFlightsAction),
        ("show my wishlist in EUR", ShowWishlistAction),
        ("arată-mi wishlist-ul în EUR", ShowWishlistAction),
        ("stop tracking https://example.com/item", DeleteWishlistAction),
        ("setează prețul țintă pentru https://example.com/item la 100 EUR", WishlistTargetAction),
        ("șterge prețul țintă pentru https://example.com/item", ClearWishlistTargetAction),
        ("enable restock only for https://example.com/item", WishlistRestockAction),
        ("actualizează wishlist-ul meu", RefreshWishlistAction),
        ("graph https://example.com/item in EUR for 30 days", WishlistGraphAction),
        ("compară wishlist-ul meu pentru 30 zile", WishlistGraphAction),
        ("remind me in two hours to stretch", ReminderAction),
        ("amintește-mi peste 3 zile să sun acasă", ReminderAction),
    ],
)
def test_parser_supported_anchored_phrases(text, kind):
    assert isinstance(parse_natural_command(text), kind)


def test_parser_falls_through_unsupported_prose_and_rejects_trailing_text():
    assert parse_natural_command("what flights should I take?") is None
    assert isinstance(
        parse_natural_command("track https://example.com/item and summarize it"),
        NaturalCommandError,
    )


def test_track_reply_requires_exactly_one_url():
    parsed = parse_natural_command("track this", replied_text="See https://example.com/item")
    assert parsed == TrackURLAction("https://example.com/item")
    ambiguous = parse_natural_command(
        "track this", replied_text="https://one.example/a https://two.example/b"
    )
    assert isinstance(ambiguous, NaturalCommandError)
    assert "multiple" in ambiguous.message


def test_relative_reminder_requires_positive_supported_duration():
    assert isinstance(parse_natural_command("remind me in 0 hours to wake up"), NaturalCommandError)
    assert parse_natural_command("remind me in 2 weeks to wake up").seconds == 14 * 86400


def test_reminder_is_created_immediately_without_public_confirmation(monkeypatch):
    message = SimpleNamespace(
        author=SimpleNamespace(id=10),
        channel=SimpleNamespace(id=20),
        reference=None,
        reply=AsyncMock(),
        add_reaction=AsyncMock(),
    )
    monkeypatch.setattr(
        "features.natural_commands.extract_mention_text",
        lambda _message, _bot_id: "remind me in 2 hours to stretch",
    )
    add_reminder = MagicMock(return_value=42)
    reminders = SimpleNamespace(create_reminder_at=add_reminder)
    feature = NaturalCommandsFeature(None, bot_id=99, wishlist=None, flights=None, reminders=reminders)

    assert asyncio.run(feature.handle_message(message)) is True
    add_reminder.assert_called_once()
    message.reply.assert_not_awaited()
    message.add_reaction.assert_awaited_once_with("✅")


def test_reminder_database_failure_is_generic(monkeypatch):
    message = SimpleNamespace(
        author=SimpleNamespace(id=10), channel=SimpleNamespace(id=20),
        reference=None, reply=AsyncMock(), add_reaction=AsyncMock(),
    )
    monkeypatch.setattr(
        "features.natural_commands.extract_mention_text",
        lambda *_: "remind me in 2 hours to private details",
    )
    reminders = SimpleNamespace(create_reminder_at=MagicMock(return_value=None))
    feature = NaturalCommandsFeature(None, bot_id=99, wishlist=None, flights=None, reminders=reminders)

    assert asyncio.run(feature.handle_message(message)) is True
    assert "private details" not in message.reply.await_args.args[0]
    message.add_reaction.assert_not_awaited()


def test_natural_flight_result_is_private(monkeypatch):
    author = SimpleNamespace(id=10, send=AsyncMock())
    message = SimpleNamespace(
        author=author, channel=SimpleNamespace(id=20),
        reference=None, reply=AsyncMock(), add_reaction=AsyncMock(),
    )
    monkeypatch.setattr("features.natural_commands.extract_mention_text", lambda *_: "show my flights")
    monkeypatch.setattr(
        "features.natural_commands.format_user_flight_trackers",
        lambda user_id, **kwargs: [f"private flights for {user_id}"],
    )
    feature = NaturalCommandsFeature(None, bot_id=99, wishlist=None, flights=None, reminders=None)
    assert asyncio.run(feature.handle_message(message)) is True
    author.send.assert_awaited_once()
    assert author.send.await_args.args == ("private flights for 10",)
    message.reply.assert_not_awaited()
    message.add_reaction.assert_awaited_once_with("✅")


def test_natural_track_executes_once_and_only_reacts_on_success(monkeypatch):
    author = SimpleNamespace(id=10, send=AsyncMock())
    message = SimpleNamespace(
        author=author, channel=SimpleNamespace(id=20), reference=None,
        reply=AsyncMock(), add_reaction=AsyncMock(),
    )
    monkeypatch.setattr("features.natural_commands.extract_mention_text", lambda *_: "track https://example.com/item")
    wishlist = SimpleNamespace(add_item_for_user=AsyncMock(return_value=SimpleNamespace(status="added")))
    feature = NaturalCommandsFeature(None, bot_id=99, wishlist=wishlist, flights=None, reminders=None)
    assert asyncio.run(feature.handle_message(message)) is True
    wishlist.add_item_for_user.assert_awaited_once_with(10, "https://example.com/item", language="en")
    message.add_reaction.assert_awaited_once_with("✅")
    message.reply.assert_not_awaited()


def test_private_delivery_failure_never_posts_result_publicly(monkeypatch):
    author = SimpleNamespace(id=10, send=AsyncMock(side_effect=RuntimeError("blocked")))
    message = SimpleNamespace(
        author=author, channel=SimpleNamespace(id=20), reference=None,
        reply=AsyncMock(), add_reaction=AsyncMock(),
    )
    monkeypatch.setattr("features.natural_commands.extract_mention_text", lambda *_: "show my flights")
    monkeypatch.setattr("features.natural_commands.format_user_flight_trackers", lambda _id: ["secret flight details"])
    feature = NaturalCommandsFeature(None, bot_id=99, wishlist=None, flights=None, reminders=None)
    assert asyncio.run(feature.handle_message(message)) is True
    assert "secret flight details" not in str(message.reply.await_args)
    message.add_reaction.assert_not_awaited()


def test_wishlist_add_distinguishes_existing_row_from_database_failure(monkeypatch):
    from wishlist.scraper import ScrapeResult

    feature = object.__new__(WishlistFeature)
    feature.scraper = SimpleNamespace(
        fetch=lambda _url: ScrapeResult(10.0, True, "Item", "EUR")
    )
    monkeypatch.setattr("features.wishlist.db.add_scraped_item", lambda *args, **kwargs: None)
    existing = asyncio.run(feature.add_item_for_user(10, "https://example.com/item"))
    monkeypatch.setattr("features.wishlist.db.add_scraped_item", lambda *args, **kwargs: False)
    failed = asyncio.run(feature.add_item_for_user(10, "https://example.com/item"))
    assert existing.status == "exists"
    assert failed.status == "database-error"
