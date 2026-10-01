import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from features.natural_commands import NaturalCommandConfirmView, NaturalCommandsFeature
from features.wishlist import WishlistFeature
from natural_commands import (
    NaturalCommandError,
    ReminderAction,
    ShowFlightsAction,
    TrackURLAction,
    parse_natural_command,
)


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("track https://example.com/item", TrackURLAction),
        ("urmărește https://example.com/item", TrackURLAction),
        ("show my flights", ShowFlightsAction),
        ("arată-mi zborurile mele", ShowFlightsAction),
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
    assert isinstance(parse_natural_command("remind me in 2 weeks to wake up"), NaturalCommandError)


def _interaction(user_id, proposal=None):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        message=proposal,
    )


def test_confirmation_is_requester_only_and_one_shot():
    execute = AsyncMock(return_value="done")
    view = NaturalCommandConfirmView(10, execute)
    outsider = _interaction(11)
    assert asyncio.run(view.interaction_check(outsider)) is False
    assert outsider.response.send_message.await_args.kwargs["ephemeral"] is True

    owner = _interaction(10)
    asyncio.run(view._confirm(owner))
    asyncio.run(view._confirm(_interaction(10)))
    execute.assert_awaited_once()
    assert owner.followup.send.await_args.kwargs["ephemeral"] is True


def test_reminder_has_no_write_before_confirmation(monkeypatch):
    proposal = SimpleNamespace(edit=AsyncMock())
    message = SimpleNamespace(
        author=SimpleNamespace(id=10),
        channel=SimpleNamespace(id=20),
        reference=None,
        reply=AsyncMock(return_value=proposal),
    )
    monkeypatch.setattr(
        "features.natural_commands.extract_mention_text",
        lambda _message, _bot_id: "remind me in 2 hours to stretch",
    )
    monkeypatch.setattr("features.natural_commands.db.get_assistant_profile", lambda _id: None)
    add_reminder = MagicMock(return_value=42)
    reminders = SimpleNamespace(create_reminder_at=add_reminder)
    feature = NaturalCommandsFeature(None, bot_id=99, wishlist=None, flights=None, reminders=reminders)

    assert asyncio.run(feature.handle_message(message)) is True
    add_reminder.assert_not_called()
    view = message.reply.await_args.kwargs["view"]
    owner = _interaction(10, proposal)
    asyncio.run(view._confirm(owner))
    add_reminder.assert_called_once()
    assert owner.followup.send.await_args.kwargs["ephemeral"] is True


def test_natural_flight_result_is_private(monkeypatch):
    proposal = SimpleNamespace(edit=AsyncMock())
    message = SimpleNamespace(
        author=SimpleNamespace(id=10), channel=SimpleNamespace(id=20),
        reference=None, reply=AsyncMock(return_value=proposal),
    )
    monkeypatch.setattr("features.natural_commands.extract_mention_text", lambda *_: "show my flights")
    monkeypatch.setattr("features.natural_commands.db.get_assistant_profile", lambda _id: None)
    monkeypatch.setattr(
        "features.natural_commands.format_user_flight_trackers",
        lambda user_id: [f"private flights for {user_id}"],
    )
    feature = NaturalCommandsFeature(None, bot_id=99, wishlist=None, flights=None, reminders=None)
    asyncio.run(feature.handle_message(message))
    view = message.reply.await_args.kwargs["view"]
    assert view.children[0].label == "View my flights"
    owner = _interaction(10, proposal)
    asyncio.run(view._confirm(owner))
    assert owner.followup.send.await_args.args == ("private flights for 10",)
    assert owner.followup.send.await_args.kwargs["ephemeral"] is True


def test_wishlist_add_distinguishes_existing_row_from_database_failure(monkeypatch):
    from wishlist.scraper import ScrapeResult

    feature = object.__new__(WishlistFeature)
    feature.scraper = SimpleNamespace(
        fetch=lambda _url: ScrapeResult(10.0, True, "Item", "EUR")
    )
    monkeypatch.setattr("features.wishlist.db.add_scraped_item", lambda *args: None)
    existing = asyncio.run(feature.add_item_for_user(10, "https://example.com/item"))
    monkeypatch.setattr("features.wishlist.db.add_scraped_item", lambda *args: False)
    failed = asyncio.run(feature.add_item_for_user(10, "https://example.com/item"))
    assert existing.status == "exists"
    assert failed.status == "database-error"
