import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from features.natural_commands import NaturalCommandsFeature, NaturalCommandSuggestionView
from features.wishlist import WishlistFeature
from natural_commands import (
    NaturalCommandError,
    NaturalClarification,
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
    suggest_natural_command,
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


@pytest.mark.parametrize(
    ('text', 'canonical', 'action'),
    [
        ('Arata-mi graficul listei de dorinte', 'grafic pentru wishlist-ul meu', WishlistGraphAction()),
        ('  ARATĂ-MI\tGRAFICUL  LISTEI DE DORINȚE! ', 'grafic pentru wishlist-ul meu', WishlistGraphAction()),
        ('afișează-mi grafic lista de dorințe mea', 'grafic pentru wishlist-ul meu', WishlistGraphAction()),
        ('graficul wishlist-ul meu', 'grafic pentru wishlist-ul meu', WishlistGraphAction()),
        ('lista dorinte', 'arată-mi lista de dorințe', ShowWishlistAction()),
        ('LISTA  DORINȚE MEA', 'arată-mi lista de dorințe', ShowWishlistAction()),
        ('arata-mi lista dorinte', 'arată-mi lista de dorințe', ShowWishlistAction()),
        ('afiseaza-mi lista de dorinte', 'arată-mi lista de dorințe', ShowWishlistAction()),
        ('wish list', 'show my wishlist', ShowWishlistAction()),
        ('show me my wish list', 'show my wishlist', ShowWishlistAction()),
        ('show me the graph of my wishlist', 'graph my wishlist', WishlistGraphAction()),
        ('show me the graph of my wish list', 'graph my wishlist', WishlistGraphAction()),
        ('graficul listei de dorinte în euro pentru 30 zile', 'grafic pentru wishlist-ul meu în euro pentru 30 zile', WishlistGraphAction(currency='EUR', days=30)),
        ('show me the graph of my wishlist in EUR for 45 days', 'graph my wishlist in EUR for 45 days', WishlistGraphAction(currency='EUR', days=45)),
        ('lista dorinte în lei', 'arată-mi lista de dorințe în lei', ShowWishlistAction('RON')),
        ('show me my wish list in GBP', 'show my wishlist in GBP', ShowWishlistAction('GBP')),
        ('could you remind me in 20 minutes to stretch', 'remind me in 20 minutes to stretch', ReminderAction(1200, 20, 'minutes', 'stretch')),
        ('can you show my wishlist', 'show my wishlist', ShowWishlistAction()),
        ('would you graph my wishlist', 'graph my wishlist', WishlistGraphAction()),
        ('poți să arată-mi lista de dorințe', 'arată-mi lista de dorințe', ShowWishlistAction()),
        ('poti arata-mi wishlist-ul', 'arata-mi wishlist-ul', ShowWishlistAction()),
        ('ai putea să arată-mi wishlist-ul', 'arată-mi wishlist-ul', ShowWishlistAction()),
    ],
)
def test_suggestion_has_complete_canonical_and_intended_action(text, canonical, action):
    assert suggest_natural_command(text) == canonical
    assert parse_natural_command(canonical) == action


@pytest.mark.parametrize('text', [
    'how are you?', 'show me a poem', 'what flights should I take?',
    'what is a wishlist?', 'why should I graph my wishlist?',
    '"lista dorinte"', '`show me the graph of my wishlist`',
    'For example: lista dorinte', 'nu lista dorinte',
    "don't show me my wish list", 'could you not graph my wishlist',
    'could you why graph my wishlist', 'could you "graph my wishlist"',
    'could you graph my wishlist and show my flights',
    'could you remind me in 20 minutes to stretch and then show my flights',
    'lista dorinte and show my flights', 'lista dorinte because I am curious',
    'show me the graph of my wishlist and explain it',
    'arata-mi graficul listei de dorinte apoi arata-mi zborurile',
    'lista dorinte in XYZ', 'graficul listei de dorinte pentru 181 zile',
    'graficul listei de dorinte pentru 0 zile', 'graficul listei de dorinte in EUR extra',
    'could you lista dorinte', 'could you set target price for Widget to -2 EUR',
])
def test_suggestion_rejects_unsupported_or_unsafe_full_messages(text):
    assert suggest_natural_command(text) is None


@pytest.mark.parametrize('command', [
    'track https://example.com/ȚEST?Token=AbC&Price=1.250,50',
    'remind me in 20 minutes to Read https://example.com/Știri?Key=AbC at 12:30 on 2026-10-09; price 1.250,50 EUR',
    'set target price for https://example.com/AbC to 100 EUR',
])
def test_modal_suggestion_preserves_original_operands(command):
    canonical = suggest_natural_command('Could\tYOU  ' + command)
    assert canonical == command
    assert parse_natural_command(canonical) == parse_natural_command(command)


def test_modal_suggestion_retains_reply_and_existing_clarification():
    canonical = suggest_natural_command('can you track this', replied_text='https://example.com/Case')
    assert parse_natural_command(canonical, replied_text='https://example.com/Case') == TrackURLAction('https://example.com/Case')
    canonical = suggest_natural_command('could you remind me to stretch')
    assert isinstance(parse_natural_command(canonical), NaturalClarification)


@pytest.fixture
def suggestion_feature(tmp_db):
    wishlist = SimpleNamespace(
        add_item_for_user=AsyncMock(return_value=SimpleNamespace(status='added')),
        send_graph_for_user=AsyncMock(),
        format_items_for_user=MagicMock(return_value=[]),
    )
    reminders = SimpleNamespace(create_reminder_at=MagicMock(return_value=42))
    feature = NaturalCommandsFeature(None, bot_id=99, wishlist=wishlist, flights=None, reminders=reminders)
    feature._outcome = AsyncMock()
    return feature


def suggestion_message(text, *, message_id=100, mention=True, replied_text=''):
    return SimpleNamespace(
        id=message_id,
        content='<@99> ' + text if mention else text,
        mentions=[SimpleNamespace(id=99)] if mention else [],
        role_mentions=[], channel_mentions=[],
        author=SimpleNamespace(id=10, bot=False, send=AsyncMock()),
        channel=SimpleNamespace(id=20), guild=None,
        reference=SimpleNamespace(resolved=SimpleNamespace(clean_content=replied_text)) if replied_text else None,
        reply=AsyncMock(), add_reaction=AsyncMock(),
    )


def suggestion_interaction(user_id=10):
    return SimpleNamespace(user=SimpleNamespace(id=user_id), response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()))


@pytest.mark.parametrize('enabled', ['0', '1'])
@pytest.mark.parametrize('text', ['Arata-mi graficul listei de dorinte', 'lista dorinte', 'could you remind me in 20 minutes to stretch'])
def test_suggestion_consumes_dispatch_without_llm_or_action(suggestion_feature, monkeypatch, enabled, text):
    monkeypatch.setenv('NATURAL_LLM_ENABLED', enabled)
    message = suggestion_message(text)
    later_llm = SimpleNamespace(handle_message=AsyncMock())
    suggestion_feature.execute_parsed = AsyncMock()

    async def dispatch():
        for handler in (suggestion_feature, later_llm):
            if await handler.handle_message(message):
                return

    asyncio.run(dispatch())
    later_llm.handle_message.assert_not_awaited()
    suggestion_feature.execute_parsed.assert_not_awaited()
    suggestion_feature.wishlist.add_item_for_user.assert_not_awaited()
    suggestion_feature.wishlist.send_graph_for_user.assert_not_awaited()
    suggestion_feature.reminders.create_reminder_at.assert_not_called()
    message.add_reaction.assert_not_awaited()
    message.author.send.assert_not_awaited()
    message.reply.assert_awaited_once()
    prompt = message.reply.await_args
    view = prompt.kwargs['view']
    assert isinstance(view, NaturalCommandSuggestionView)
    assert view.canonical in prompt.args[0]
    assert [child.label for child in view.children] == (['Yes', 'No'] if view.language == 'en' else ['Da', 'Nu'])
    assert view.timeout == 600
    assert prompt.kwargs['mention_author'] is False
    assert prompt.kwargs['suppress_embeds'] is True
    assert prompt.kwargs['allowed_mentions'].to_dict() == {'parse': []}
    suggestion_feature._outcome.assert_awaited_once_with(message, 'unknown', view.language, 'parser', 'clarification')


def test_confirmation_executes_once_with_original_message_and_no_cached_profile(suggestion_feature):
    message = suggestion_message('could you track this', replied_text='https://example.com/Case')
    suggestion_feature.execute_parsed = AsyncMock()

    async def confirm():
        assert await suggestion_feature.handle_message(message)
        view = message.reply.await_args.kwargs['view']
        first, duplicate = suggestion_interaction(), suggestion_interaction()
        await asyncio.gather(view.confirm(first), view.confirm(duplicate))
        assert view.consumed and view.is_finished() and view.children == []
        first.response.edit_message.assert_awaited_once_with(view=None)
        duplicate.response.send_message.assert_awaited_once()
        assert duplicate.response.send_message.await_args.kwargs['ephemeral'] is True
        await view.confirm(suggestion_interaction())

    asyncio.run(confirm())
    suggestion_feature.execute_parsed.assert_awaited_once_with(message, TrackURLAction('https://example.com/Case'), language='en')


def test_suggestion_yes_preserves_dedup_and_requester_for_reminder(suggestion_feature):
    message = suggestion_message('could you remind me in 20 minutes to stretch')

    async def confirm():
        assert await suggestion_feature.handle_message(message)
        view = message.reply.await_args.kwargs['view']
        await asyncio.gather(view.confirm(suggestion_interaction()), view.confirm(suggestion_interaction()))
        await suggestion_feature.execute_parsed(message, parse_natural_command(view.canonical), language='en')

    asyncio.run(confirm())
    suggestion_feature.reminders.create_reminder_at.assert_called_once()
    args = suggestion_feature.reminders.create_reminder_at.call_args
    assert args.args[:2] == (10, 20)
    assert args.args[3] == 'stretch'
    assert args.kwargs['creator_id'] == 10
    message.add_reaction.assert_awaited_once_with('✅')


def test_rejection_keeps_pending_and_does_not_execute(suggestion_feature):
    message = suggestion_message('lista dorinte')
    waiting = (float('inf'), NaturalClarification('currency', 'target', {}, 'currency'), None, 'en', None, 'parser')
    suggestion_feature.pending[(10, 20)] = waiting
    suggestion_feature.execute_parsed = AsyncMock()

    async def reject():
        assert await suggestion_feature.handle_message(message)
        view = message.reply.await_args.kwargs['view']
        no, duplicate = suggestion_interaction(), suggestion_interaction()
        await asyncio.gather(view.cancel(no), view.confirm(duplicate))
        assert no.response.edit_message.await_args.kwargs['content'] == 'Nu am executat nicio acțiune.'
        assert no.response.edit_message.await_args.kwargs['view'] is None

    asyncio.run(reject())
    assert suggestion_feature.pending[(10, 20)] is waiting
    suggestion_feature.execute_parsed.assert_not_awaited()
    message.add_reaction.assert_not_awaited()
    suggestion_feature._outcome.assert_any_await(message, 'unknown', 'ro', 'parser', 'abandoned')


@pytest.mark.parametrize('button', ['confirm', 'cancel'])
def test_other_user_cannot_consume_suggestion(suggestion_feature, button):
    message = suggestion_message('show me the graph of my wishlist')
    suggestion_feature.execute_parsed = AsyncMock()

    async def press():
        await suggestion_feature.handle_message(message)
        view = message.reply.await_args.kwargs['view']
        interaction = suggestion_interaction(11)
        await getattr(view, button)(interaction)
        assert view.consumed is False
        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args.args[0] == 'This control belongs to another user.'
        assert interaction.response.send_message.await_args.kwargs['ephemeral'] is True
        interaction.response.edit_message.assert_not_awaited()

    asyncio.run(press())
    suggestion_feature.execute_parsed.assert_not_awaited()


@pytest.mark.parametrize('expiration', ['deadline', 'timeout'])
@pytest.mark.parametrize('button', ['confirm', 'cancel'])
def test_expired_and_direct_post_timeout_callbacks_never_execute(suggestion_feature, expiration, button):
    message = suggestion_message('wish list')
    suggestion_feature.execute_parsed = AsyncMock()

    async def press():
        await suggestion_feature.handle_message(message)
        view = message.reply.await_args.kwargs['view']
        if expiration == 'deadline':
            view.deadline = 0
        else:
            await view.on_timeout()
            assert view.is_finished()
        interaction = suggestion_interaction()
        await getattr(view, button)(interaction)
        interaction.response.edit_message.assert_not_awaited()
        assert 'expired' in interaction.response.send_message.await_args.args[0]

    asyncio.run(press())
    suggestion_feature.execute_parsed.assert_not_awaited()
    message.add_reaction.assert_not_awaited()


@pytest.mark.parametrize('field, slots, response', [
    ('currency', {'reference': 'Widget', 'price': 10}, 'EUR'),
    ('price', {'reference': 'Widget', 'currency': 'EUR'}, '20'),
    ('when', {'text': 'stretch'}, 'in 20 minutes'),
    ('timezone', {'when': 'tomorrow at 10:00', 'text': 'stretch'}, 'Europe/Bucharest'),
    ('text', {'when': 'in 20 minutes'}, 'text: stretch'),
])
def test_valid_pending_clarification_precedes_suggestion(suggestion_feature, monkeypatch, field, slots, response):
    suggestion_feature.pending[(10, 20)] = (float('inf'), NaturalClarification(field, 'reminder' if field == 'when' else 'target', slots, field), None, 'en', None, 'parser')
    message = suggestion_message(response)
    suggestion_feature.execute_parsed = AsyncMock(return_value=True)
    suggester = MagicMock(side_effect=AssertionError('valid clarification must win'))
    monkeypatch.setattr('features.natural_commands.suggest_natural_command', suggester)
    assert asyncio.run(suggestion_feature.handle_message(message)) is True
    suggester.assert_not_called()
    suggestion_feature.execute_parsed.assert_awaited_once()
    message.reply.assert_not_awaited()
    assert (10, 20) not in suggestion_feature.pending


@pytest.mark.parametrize('field', ['timezone', 'currency', 'price', 'reference', 'when', 'text'])
@pytest.mark.parametrize('text', ['lista dorinte', 'Arata-mi graficul listei de dorinte', 'how are you?'])
def test_invalid_pending_fields_offer_near_misses_and_keep_pending(suggestion_feature, field, text):
    waiting = (float('inf'), NaturalClarification(field, 'track' if field == 'reference' else 'reminder', {}, field), None, 'en', None, 'parser')
    suggestion_feature.pending[(10, 20)] = waiting
    message = suggestion_message(text)
    suggestion_feature.execute_parsed = AsyncMock()
    assert asyncio.run(suggestion_feature.handle_message(message)) is (text != 'how are you?')
    assert suggestion_feature.pending[(10, 20)] is waiting
    suggestion_feature.execute_parsed.assert_not_awaited()
    assert message.reply.await_count == (0 if text == 'how are you?' else 1)


def test_recognized_command_still_executes_without_confirmation(suggestion_feature):
    message = suggestion_message('remind me in 20 minutes to stretch')
    assert asyncio.run(suggestion_feature.handle_message(message)) is True
    suggestion_feature.reminders.create_reminder_at.assert_called_once()
    message.reply.assert_not_awaited()
    message.add_reaction.assert_awaited_once_with('✅')


def test_unmentioned_near_miss_is_not_consumed(suggestion_feature):
    message = suggestion_message('lista dorinte', mention=False)
    assert asyncio.run(suggestion_feature.handle_message(message)) is False
    message.reply.assert_not_awaited()


def test_confirmation_failure_uses_existing_error_without_success(suggestion_feature):
    message = suggestion_message('could you remind me in 20 minutes to stretch')
    suggestion_feature.reminders.create_reminder_at.return_value = None

    async def confirm():
        await suggestion_feature.handle_message(message)
        view = message.reply.await_args.kwargs['view']
        await view.confirm(suggestion_interaction())

    asyncio.run(confirm())
    assert message.reply.await_count == 2
    assert message.reply.await_args.args[0] == "I couldn't save that reminder. Please try again."
    message.add_reaction.assert_not_awaited()


def test_suggestion_too_long_consumes_without_controls_or_action(suggestion_feature):
    message = suggestion_message('could you remind me in 20 minutes to ' + 'stretch ' * 300)
    suggestion_feature.execute_parsed = AsyncMock()
    assert asyncio.run(suggestion_feature.handle_message(message)) is True
    assert '/natural-language' in message.reply.await_args.args[0]
    assert 'view' not in message.reply.await_args.kwargs
    assert len(message.reply.await_args.args[0]) <= 2000
    suggestion_feature.execute_parsed.assert_not_awaited()
    message.add_reaction.assert_not_awaited()


def test_independent_suggestions_require_independent_confirmation(suggestion_feature):
    first = suggestion_message('lista dorinte', message_id=100)
    second = suggestion_message('graficul listei de dorinte', message_id=101)
    suggestion_feature.execute_parsed = AsyncMock()

    async def confirm():
        await suggestion_feature.handle_message(first)
        await suggestion_feature.handle_message(second)
        first_view = first.reply.await_args.kwargs['view']
        second_view = second.reply.await_args.kwargs['view']
        assert first_view is not second_view
        await first_view.confirm(suggestion_interaction())
        assert second_view.consumed is False
        suggestion_feature.execute_parsed.assert_awaited_once_with(first, ShowWishlistAction(), language='ro')
        await second_view.cancel(suggestion_interaction())

    asyncio.run(confirm())
    suggestion_feature.execute_parsed.assert_awaited_once()


def test_confirmation_reparse_failure_is_deterministic(suggestion_feature, monkeypatch):
    message = suggestion_message('wish list')
    suggestion_feature.execute_parsed = AsyncMock()

    async def confirm():
        await suggestion_feature.handle_message(message)
        view = message.reply.await_args.kwargs['view']
        monkeypatch.setattr('features.natural_commands.parse_natural_command', lambda *_args, **_kwargs: None)
        await view.confirm(suggestion_interaction())
        assert view.consumed

    asyncio.run(confirm())
    suggestion_feature.execute_parsed.assert_not_awaited()
    assert message.reply.await_count == 2
    assert '/help' in message.reply.await_args.args[0]
    message.add_reaction.assert_not_awaited()


def test_confirmation_reads_fresh_profile(suggestion_feature):
    import db
    message = suggestion_message('could you remind me in 20 minutes to stretch')

    async def confirm():
        await suggestion_feature.handle_message(message)
        view = message.reply.await_args.kwargs['view']
        assert db.set_assistant_profile(10, timezone='Europe/Bucharest', timezone_configured=True)
        await view.confirm(suggestion_interaction())

    asyncio.run(confirm())
    assert suggestion_feature.reminders.create_reminder_at.call_args.kwargs['timezone'] == 'Europe/Bucharest'


def test_confirmation_reuses_existing_ten_minute_clarification(suggestion_feature):
    message = suggestion_message('could you remind me to stretch')

    async def confirm():
        await suggestion_feature.handle_message(message)
        view = message.reply.await_args.kwargs['view']
        await view.confirm(suggestion_interaction())
        waiting = suggestion_feature.pending[(10, 20)]
        assert waiting[1] == NaturalClarification('when', 'reminder', {'text': 'stretch', 'recurrence': None}, 'time')
        assert abs(waiting[0] - view.deadline) < 1

    asyncio.run(confirm())
    suggestion_feature.reminders.create_reminder_at.assert_not_called()
    assert message.reply.await_count == 2
    message.add_reaction.assert_not_awaited()
