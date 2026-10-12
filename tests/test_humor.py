import asyncio
import hashlib
import sqlite3
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

import db
from features import humor
from features import humor_common as common


def interaction(guild_id=12, locale="en-US", user_id=42):
    value = MagicMock()
    value.guild_id = guild_id
    value.locale = locale
    value.user.id = user_id
    value.user.send = AsyncMock()
    value.response.defer = AsyncMock()
    value.followup.send = AsyncMock()
    return value


def feature():
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    return humor.HumorFeature(client, tree)


def test_catalogs_and_calendar_helpers():
    assert common.week_key(date(2021, 1, 1)) == "2020-W53"
    assert common.local_day() == date.today()
    assert len(common.QUESTS) >= 3
    for quest in common.QUESTS:
        assert quest["text_en"] and quest["text_ro"] and quest["title_en"] and quest["title_ro"]
    for key, values in common.CATALOG.items():
        assert len(values) == 2 and all(values)
    assert len(common.safe_text("x" * 3000, 500)) == 500
    assert "@everyone" not in common.safe_text("@everyone **hello**")
    assert "\\*" in common.safe_text("**hello**")


def test_profile_language_precedes_locale(tmp_db):
    value = interaction(locale="ro")
    assert asyncio.run(common.interaction_language(value)) == "ro"
    db.set_assistant_profile(value.user.id, language="en")
    assert asyncio.run(common.interaction_language(value)) == "en"
    db.set_assistant_profile(value.user.id, language="ro")
    value.locale = "en-US"
    assert asyncio.run(common.interaction_language(value)) == "ro"
    db.set_assistant_profile(value.user.id, language="auto")
    assert asyncio.run(common.interaction_language(value)) == "en"


def test_profile_failure_uses_locale_without_exposing_error(monkeypatch, caplog):
    getter = MagicMock(side_effect=sqlite3.OperationalError("private internals"))
    monkeypatch.setattr(db, "get_assistant_profile", getter)
    assert asyncio.run(common.interaction_language(interaction(locale="ro"))) == "ro"
    assert "private internals" not in caplog.text


@pytest.mark.parametrize("language", ["en", "ro"])
@pytest.mark.parametrize("style", list(common.EXCUSE_STYLES))
@pytest.mark.parametrize("guild_id", [12, None])
def test_excuses_bilingual_styles_and_private_delivery(tmp_db, monkeypatch, language, style, guild_id):
    value = interaction(guild_id=guild_id, locale=language)
    generate = AsyncMock(side_effect=lambda kind, language, facts, fallback: fallback)
    monkeypatch.setattr(humor, "generate_humor", generate)
    asyncio.run(feature().excuse(value, "private reason", style))
    value.response.defer.assert_awaited_once_with(ephemeral=True)
    value.user.send.assert_not_called()
    value.followup.send.assert_awaited_once()
    sent = value.followup.send.call_args.args[0]
    assert sent == common.EXCUSE_FALLBACKS[style][language == "ro"].format(reason="private reason")
    assert len(sent) <= 2000
    assert value.followup.send.call_args.kwargs["allowed_mentions"].everyone is False
    assert value.followup.send.call_args.kwargs["suppress_embeds"] is True
    assert value.followup.send.call_args.kwargs["ephemeral"] is True
    generate.assert_awaited_once()


def test_excuse_styles_are_distinct():
    for language in ("en", "ro"):
        assert len({values[language == "ro"] for values in common.EXCUSE_FALLBACKS.values()}) == 4


@pytest.mark.parametrize("reason", ["", "  ", "x" * 501])
def test_excuse_reason_bound(tmp_db, monkeypatch, reason):
    generate = AsyncMock()
    monkeypatch.setattr(humor, "generate_humor", generate)
    value = interaction()
    asyncio.run(feature().excuse(value, reason, "corporate"))
    generate.assert_not_called()
    value.user.send.assert_not_called()
    assert value.followup.send.call_args.kwargs["ephemeral"] is True


@pytest.mark.parametrize("language", ["en", "ro"])
@pytest.mark.parametrize("command", ["excuse", "worth-it"])
def test_blocked_dm_does_not_prevent_ephemeral_result(tmp_db, monkeypatch, language, command):
    value = interaction(locale=language)
    response = MagicMock(status=403, reason="Forbidden")
    value.user.send.side_effect = discord.Forbidden(response, "blocked")
    monkeypatch.setattr(humor, "generate_humor", AsyncMock(return_value="secret generated result"))
    worker = feature()
    if command == "excuse":
        asyncio.run(worker.excuse(value, "private reason", "corporate"))
    else:
        db.add_scraped_item(value.user.id, "https://private.example", title="secret title", price=100, currency="RON")
        asyncio.run(worker.purchase(value, "https://private.example"))
    value.user.send.assert_not_called()
    value.response.defer.assert_awaited_once_with(ephemeral=True)
    value.followup.send.assert_awaited_once()
    private = value.followup.send.call_args.args[0]
    assert "secret generated result" in private
    if command == "worth-it":
        assert "secret title" in private
    assert value.followup.send.call_args.kwargs["ephemeral"] is True
    assert value.followup.send.call_args.kwargs["allowed_mentions"].everyone is False
    assert value.followup.send.call_args.kwargs["suppress_embeds"] is True


def test_purchase_owned_saved_data_only(tmp_db, monkeypatch):
    value = interaction()
    url = "https://saved.example/item"
    item_id = db.add_scraped_item(value.user.id, url, title="Desk", price=100, currency="RON", record_history=True)
    db.add_price_history(item_id, 80)
    db.add_price_history(item_id, 120)
    db.add_scraped_item(99, url, title="Other member's secret", price=999, currency="USD", record_history=True)
    before = db.get_scraped_item(value.user.id, url), db.get_price_history(value.user.id, url)
    get_item = MagicMock(wraps=db.get_scraped_item)
    get_history = MagicMock(wraps=db.get_price_history)
    monkeypatch.setattr(db, "get_scraped_item", get_item)
    monkeypatch.setattr(db, "get_price_history", get_history)
    generate = AsyncMock(side_effect=lambda kind, language, facts, fallback: fallback)
    monkeypatch.setattr(humor, "generate_humor", generate)
    asyncio.run(feature().purchase(value, url))
    get_item.assert_called_once_with(value.user.id, url)
    get_history.assert_called_once_with(value.user.id, url)
    value.user.send.assert_not_called()
    value.response.defer.assert_awaited_once_with(ephemeral=True)
    value.followup.send.assert_awaited_once()
    assert value.followup.send.call_args.kwargs["ephemeral"] is True
    sent = value.followup.send.call_args.args[0]
    assert "100.00" in sent and "80.00" in sent and "RON" in sent
    assert "Other member" not in sent and "999" not in sent
    assert "Lowest:" in sent and "Highest:" in sent and "Median:" in sent
    assert "Not enough data" in sent
    assert "Purchase advocate" in sent and "Household accountant" in sent
    assert before == (get_item(value.user.id, url), get_history(value.user.id, url))


def test_purchase_wrong_owner_does_not_generate(tmp_db, monkeypatch):
    db.add_scraped_item(99, "https://saved.example", title="secret", price=100)
    generate = AsyncMock()
    monkeypatch.setattr(humor, "generate_humor", generate)
    value = interaction()
    asyncio.run(feature().purchase(value, "https://saved.example"))
    generate.assert_not_called()
    value.user.send.assert_not_called()
    assert value.followup.send.call_args.args[0] == common.text("wishlist_missing", "en")


@pytest.mark.parametrize("language", ["en", "ro"])
def test_sparse_invalid_history_and_missing_facts(language):
    item = (1, 42, "url", None, None, "Desk", None, None, None, None, None, 0, 0, None, None)
    rows = [(float("nan"), 1), (float("inf"), 2), (-10, 3), (0, 4), (True, 5), (80, 100), ("20", 7)]
    summary, facts = humor.purchase_summary(item, rows, language, now=200000)
    assert facts["history_samples"] == 1
    assert facts["saved_price"] is None
    assert facts["recorded_minimum"] == facts["recorded_median"] == 80
    assert "80.00" in summary
    assert facts["decision"] == "insufficient"
    assert common.text("purchase_insufficient", language) in summary
    assert "nan" not in summary and "inf" not in summary
    empty, facts = humor.purchase_summary(item, [], language)
    assert facts["recorded_minimum"] is None and facts["recorded_median"] is None
    assert common.text("unknown", language) in empty


def test_saved_observations_stale_and_median():
    item = (1, 42, "url", 50, 0, "Desk", "RON", None, None, None, None, 0, 0, 1000, None)
    summary, facts = humor.purchase_summary(item, [(30, 800), (10, 900), (20, 1000), (40, 1100)], "en", now=200000)
    assert facts["stale"] is True
    assert facts["recorded_median"] == 25
    assert facts["recorded_maximum"] == 40
    assert "10.00 RON" in summary and "40.00 RON" in summary
    assert facts["decision"] == "unavailable"
    assert "out of stock" in summary and "25.00" in summary


def test_extreme_finite_prices_do_not_overflow_reply():
    item = (1, 42, "url", 1e308, None, "x" * 500, "RON", None, None, None, None, 0, 0, None, None)
    summary, facts = humor.purchase_summary(item, [(1e308, 100), (1e308, 200)], "ro")
    assert len(summary + "\n\n" + humor.purchase_fallback("ro")) <= 2000
    assert facts["recorded_median"] == 1e308


def test_horoscope_sha256_language_only_changes_rendering():
    day = date(2026, 10, 12)
    for theme in common.HOROSCOPE_THEMES:
        digest = hashlib.sha256(f"12:42:{day.isoformat()}:{theme}".encode()).digest()
        sign = common.HOROSCOPE_SIGNS[int.from_bytes(digest[:8], "big") % len(common.HOROSCOPE_SIGNS)]
        forecasts = common.HOROSCOPE_FORECASTS[theme]
        forecast = forecasts[int.from_bytes(digest[8:16], "big") % len(forecasts)]
        for language in ("en", "ro"):
            result = humor.horoscope(12, 42, day, theme, language)
            assert sign[language == "ro"] in result and forecast[language == "ro"] in result
            assert result == humor.horoscope(12, 42, day, theme, language)


def test_horoscope_repeated_and_new_instances_no_llm_or_writes(tmp_db, monkeypatch):
    monkeypatch.setattr(humor, "local_day", lambda: date(2026, 10, 12))
    generate = AsyncMock()
    monkeypatch.setattr(humor, "generate_humor", generate)
    before = tmp_db.read_bytes()
    value = interaction()
    worker = feature()
    asyncio.run(worker.forecast(value, "gaming"))
    initial = value.followup.send.call_args.args[0]
    asyncio.run(worker.forecast(value, "gaming"))
    assert value.followup.send.call_args.args[0] == initial
    asyncio.run(feature().forecast(value, "gaming"))
    assert value.followup.send.call_args.args[0] == initial
    generate.assert_not_called()
    assert before == tmp_db.read_bytes()


def test_horoscope_rejects_dm(tmp_db, monkeypatch):
    generate = AsyncMock()
    monkeypatch.setattr(humor, "generate_humor", generate)
    value = interaction(guild_id=None, locale="ro")
    asyncio.run(feature().forecast(value))
    assert value.followup.send.call_args.args[0] == common.text("guild_only", "ro")
    assert value.followup.send.call_args.kwargs["ephemeral"] is True
    value.user.send.assert_not_called()
    generate.assert_not_called()


@pytest.mark.parametrize("language", ["en", "ro"])
def test_worth_it_user_example_recommends_waiting(language):
    item = (1, 42, "url", 360.46, 1, "Moet & Chandon Nectar Demi-Sec 0.75L", "RON", None, None, None, None, 0, 0, 200000, None)
    history = [(value, 199000) for value in [241.08] + [269.00] * 411 + [360.46]]
    summary, facts = humor.purchase_summary(item, history, language, now=200001)
    assert facts["decision"] == "wait"
    assert facts["history_samples"] == 413
    assert facts["recorded_minimum"] == 241.08
    assert facts["recorded_maximum"] == 360.46
    assert facts["recorded_median"] == 269
    assert all(value in summary for value in ("360.46 RON", "241.08 RON", "269.00 RON", "+34.0%", "+49.5%"))
    assert common.text("purchase_wait", language) in summary
    voices = humor.purchase_fallback(language, facts)
    assert "269.00 RON" in voices
    assert ("Do not buy" if language == "en" else "Nu cumpăra") in voices
    assert len(summary + "\n\n" + voices) <= 2000


@pytest.mark.parametrize("price,stock,checked,history,decision", [
    (80, 1, 200000, [80, 90, 100, 110, 120, 130, 140], "buy"),
    (95, 1, 200000, [80, 90, 100, 110, 120, 130, 140], "buy"),
    (150, 1, 200000, [80, 90, 100, 110, 120, 130, 140], "wait"),
    (110, 1, 200000, [80, 90, 100, 110, 120, 130, 140], "neutral"),
    (100, 1, 200000, [100] * 7, "neutral"),
    (150, 1, 200000, [100] * 7, "wait"),
    (80, 1, 1000, [80, 90, 100, 110, 120, 130, 140], "refresh"),
    (80, 1, None, [80, 90, 100, 110, 120, 130, 140], "refresh"),
    (80, None, 200000, [80, 90, 100, 110, 120, 130, 140], "refresh"),
    (80, 0, 200000, [80, 90, 100, 110, 120, 130, 140], "unavailable"),
    (80, 1, 200000, [80, 100], "insufficient"),
    (None, 1, 200000, [80, 90, 100, 110, 120, 130, 140], "insufficient"),
])
def test_worth_it_price_stock_and_freshness_decisions(price, stock, checked, history, decision):
    item = (1, 42, "url", price, stock, "Desk", "RON", None, None, None, None, 0, 0, checked, None)
    summary, facts = humor.purchase_summary(item, [(value, 199000) for value in history], "en", now=200001)
    assert facts["decision"] == decision
    assert common.text("purchase_" + decision, "en") in summary
    assert len(summary + "\n\n" + humor.purchase_fallback("en", facts)) <= 2000


def test_commands_and_optional_annotation():
    worker = feature()
    assert {cmd.name for cmd in worker.tree.get_commands()} == {"excuse", "worth-it", "horoscope"}
    command = worker.tree.get_command("horoscope")
    assert command.parameters[0].required is False
    assert "Optional" in str(command.callback.__annotations__["theme"])
    assert len(worker.tree.get_command("excuse").parameters[1].choices) == 4


def test_command_registration_scopes_match_dm_behavior():
    worker = feature()
    horoscope_command = worker.tree.get_command("horoscope")
    assert horoscope_command.guild_only is True
    assert horoscope_command.to_dict(worker.tree)["dm_permission"] is False
    for name in ("excuse", "worth-it"):
        command = worker.tree.get_command(name)
        assert command.guild_only is False
        assert command.to_dict(worker.tree)["dm_permission"] is True
