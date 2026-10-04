import asyncio
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from assistant_profiles import AssistantProfile
from command_time import reminder_time
from features.natural_language_help import (
    CATEGORY_LABELS, EXAMPLES, NaturalLanguageHelpView, display_language, guide_content,
)
from natural_commands import (
    CalendarReminderAction, ClearWishlistTargetAction, DeleteWishlistAction,
    RefreshWishlistAction, ReminderAction, ReminderManageAction, ShowFlightsAction,
    ShowWishlistAction, TrackURLAction, WishlistGraphAction, WishlistRestockAction,
    WishlistTargetAction, parse_natural_command,
)


URL = "https://example.com/product"
NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc).timestamp()


def expected_actions(language):
    romanian = language == "ro"
    return {
        "reminders": (
            ReminderAction(1200, 20, "minutes", "fac mișcare" if romanian else "stretch"),
            CalendarReminderAction("maine la 09:00" if romanian else "tomorrow at 09:00", "sun acasă" if romanian else "call home"),
            CalendarReminderAction("la 09:00" if romanian else "at 09:00", "beau apă" if romanian else "drink water", "daily"),
            CalendarReminderAction("la 09:00" if romanian else "at 09:00", "fac mișcare" if romanian else "stretch", "weekdays"),
            CalendarReminderAction("la 09:00" if romanian else "at 09:00", "sun acasă" if romanian else "call home", "weekly"),
            ReminderManageAction("list"), ReminderManageAction("cancel", 42), ReminderManageAction("snooze", 42),
        ),
        "wishlist": (
            TrackURLAction(URL), TrackURLAction(URL), ShowWishlistAction("EUR"),
            DeleteWishlistAction("42"), WishlistTargetAction("42", 100, "EUR"),
            ClearWishlistTargetAction("42"), WishlistRestockAction("42", True),
            WishlistRestockAction("42", False),
        ),
        "prices": (
            RefreshWishlistAction(URL), RefreshWishlistAction(), WishlistGraphAction(URL, "EUR", 30),
            WishlistGraphAction(currency="EUR", days=30), WishlistGraphAction(days=30, percentage=True),
        ),
        "flights": (ShowFlightsAction(),),
    }


EXAMPLE_CASES = [
    (language, category, example, expected_actions(language)[category][index])
    for language, categories in EXAMPLES.items()
    for category, examples in categories.items()
    for index, example in enumerate(examples)
]


@pytest.mark.parametrize("language,category,example,expected", EXAMPLE_CASES)
def test_every_published_example_parses_exactly(language, category, example, expected, monkeypatch):
    monkeypatch.setattr("command_time.time.time", lambda: NOW)
    # Reply examples explicitly carry exactly one URL; no invisible recent context.
    replied_text = f"Product: {URL}" if example in ("track this", "urmărește asta") else ""
    parsed = parse_natural_command(example, replied_text=replied_text)
    assert parsed == expected
    assert f"<@999888777> `{example}`" in guide_content(language, category, bot_mention="<@999888777>")
    if isinstance(parsed, CalendarReminderAction):
        profile = AssistantProfile(timezone="Europe/Bucharest", timezone_configured=True)
        assert reminder_time(parsed.when, profile.timezone, recurrence=parsed.recurrence) > NOW


@pytest.mark.parametrize("language", ("en", "ro"))
@pytest.mark.parametrize("category", tuple(CATEGORY_LABELS))
@pytest.mark.parametrize("mention", (None, "<@999888777777777777>"))
def test_pages_fit_one_discord_message_and_publish_only_validated_examples(language, category, mention):
    content = guide_content(language, category, bot_mention=mention)
    assert len(content) <= 1900
    assert re.findall(r"`([^`]+)`", content) == list(EXAMPLES[language].get(category, ()))
    assert "/memory-" not in content and "/llm-memory" not in content
    if mention:
        assert mention in content
        assert "@bot" not in content
    else:
        assert "@bot" in content


@pytest.mark.parametrize(
    "profile,locale,expected",
    [
        ({"language": "en"}, discord.Locale.romanian, "en"),
        ({"language": "ro"}, discord.Locale.american_english, "ro"),
        (AssistantProfile(language="ro"), "en-US", "ro"),
        ({"language": "auto"}, discord.Locale.romanian, "ro"),
        ({"language": "auto"}, "ro-RO", "ro"),
        ({"language": "auto"}, "en-GB", "en"),
        ({"language": "invalid"}, "fr", "en"),
        ({}, None, "en"),
    ],
)
def test_display_language_precedence(profile, locale, expected):
    assert display_language(SimpleNamespace(locale=locale), profile) == expected


def test_display_language_reads_saved_profile_without_creating_it(monkeypatch):
    reads = []

    def read(user_id):
        reads.append(user_id)
        return None

    monkeypatch.setattr("db.get_assistant_profile", read)
    interaction = SimpleNamespace(user=SimpleNamespace(id=10), locale=discord.Locale.romanian)
    assert display_language(interaction) == "ro"
    assert reads == [10]


def interaction_for(user_id=10):
    return SimpleNamespace(user=SimpleNamespace(id=user_id), response=SimpleNamespace(
        edit_message=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock(),
    ))


def forbid_write(*args, **kwargs):
    raise AssertionError("The guide must not mutate saved preferences or call action services")


def test_navigation_edits_single_response_and_only_guide_language(monkeypatch):
    monkeypatch.setattr("db.set_assistant_profile", forbid_write)
    monkeypatch.setattr("db.reset_assistant_profile", forbid_write)

    async def run():
        view = NaturalLanguageHelpView(10, "en", bot_mention="<@999888777>")
        back = discord.ui.Button(label="Back to setup", row=2)
        view.add_item(back)
        assert view.timeout == 600
        assert len(view.category_select.options) == len(CATEGORY_LABELS)
        for category in CATEGORY_LABELS:
            interaction = interaction_for()
            view.category_select._values = [category]
            await view.category_select.callback(interaction)
            interaction.response.edit_message.assert_awaited_once()
            sent = interaction.response.edit_message.call_args.kwargs
            assert sent["content"] == guide_content("en", category, bot_mention="<@999888777>")
            assert sent["view"] is view
            assert sent["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
            interaction.response.send_message.assert_not_awaited()
            assert back in view.children
        interaction = interaction_for()
        await next(button for button in view.children if getattr(button, "label", None) == "Română").callback(interaction)
        assert view.language == "ro" and view.category == "flights"
        assert view.category_select.placeholder == "Alege categoria"
        assert view.category_select.options[0].label == "Cum începi"
        assert interaction.response.edit_message.call_args.kwargs["view"] is view
        assert interaction.response.edit_message.call_args.kwargs["content"] == guide_content("ro", "flights", bot_mention="<@999888777>")
        interaction.response.send_message.assert_not_awaited()
        interaction.response.send_modal.assert_not_awaited()
        interaction = interaction_for()
        await next(button for button in view.children if getattr(button, "label", None) == "English").callback(interaction)
        assert view.language == "en" and view.category == "flights"
        assert view.category_select.placeholder == "Choose a category"
        view.stop()

    asyncio.run(run())


@pytest.mark.parametrize("language", ("en", "ro"))
def test_foreign_requester_cannot_navigate_or_change_language(language):
    async def run():
        view = NaturalLanguageHelpView(10, language)
        foreign = interaction_for(20)
        assert not await view.interaction_check(foreign)
        foreign.response.send_message.assert_awaited_once()
        assert foreign.response.send_message.call_args.kwargs["ephemeral"] is True
        view.category_select._values = ["wishlist"]
        foreign.response.send_message.reset_mock()
        await view.category_select.callback(foreign)
        assert view.category == "overview"
        foreign.response.edit_message.assert_not_awaited()
        foreign.response.send_message.assert_awaited_once()
        foreign.response.send_message.reset_mock()
        await view.children[2].callback(foreign)
        assert view.language == language
        foreign.response.send_message.assert_awaited_once()
        foreign.response.edit_message.assert_not_awaited()
        view.stop()

    asyncio.run(run())


def test_flights_only_documents_saved_list_and_existing_slash_commands():
    for language in EXAMPLES:
        assert len(EXAMPLES[language]["flights"]) == 1
        content = guide_content(language, "flights")
        for name in ("show", "add", "delete", "budget", "login", "logout"):
            assert f"/flight-tracker-{name}" in content


def test_invalid_language_and_category_fall_back_to_english_overview():
    assert guide_content("invalid", "unknown") == guide_content("en")
