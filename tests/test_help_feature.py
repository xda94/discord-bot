from features.help_feature import _chunk_text, build_help_text
from i18n import t


def test_help_text_matches_automatic_memory_mode():
    text = build_help_text(True)

    assert "**/llm-memory**" in text
    assert "**/memory-opt-out**" in text
    assert "**/memory-add**" not in text
    assert "**/memory-erase**" not in text
    assert "**/set-birthday** `[date]`" in text
    assert "Omit date to remove it." in text
    assert "first attempt is at 00:01" in text
    assert "failures retry every 30 minutes" in text


def test_help_text_matches_manual_memory_mode():
    text = build_help_text(False)

    assert "**/memory-add**" in text
    assert "**/memory-show**" in text
    assert "**/memory-erase**" in text
    assert "**/llm-memory**" not in text
    assert "**/memory-opt-out**" not in text
    assert "manually saved memories" in text
    assert "**/set-birthday** `[date]`" in text
    assert "Omit date to remove it." in text
    assert "first attempt is at 00:01" in text
    assert "failures retry every 30 minutes" in text


def test_help_text_chunks_stay_within_discord_limit_in_both_modes():
    for automatic in (True, False):
        assert all(len(chunk) <= 1900 for chunk in _chunk_text(build_help_text(automatic)))


def test_help_text_without_memory_lists_no_memory_commands():
    from features.help_feature import build_help_text

    text = build_help_text(automatic_memory_enabled=False, memory_disabled=True)

    assert "/memory-" not in text
    assert "/llm-memory" not in text
    assert "supplement live chat history" not in text
    assert "**@bot** (mention)" in text


def test_help_text_lists_worth_it_without_removed_commands_in_every_memory_mode():
    for automatic, disabled in ((True, False), (False, False), (False, True)):
        text = build_help_text(automatic, disabled)
        assert "/worth-it" in text
        assert "/natural-language" not in text
        assert "/tribunal" not in text
        assert "/assistant-profile" not in text


def test_help_uses_locale_when_profile_language_is_auto(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import discord
    from discord import app_commands
    from features.help_feature import HelpFeature

    monkeypatch.setattr("db.get_assistant_profile", lambda user_id: {"language": "auto"})

    async def run():
        client = discord.Client(intents=discord.Intents.none())
        tree = app_commands.CommandTree(client)
        HelpFeature(client, tree, memory_disabled=True)
        interaction = SimpleNamespace(user=SimpleNamespace(id=10), locale=discord.Locale.romanian,
                                      response=SimpleNamespace(send_message=AsyncMock()))
        await tree.get_command("help").callback(interaction)
        sent = interaction.response.send_message.call_args
        view = sent.kwargs["view"]
        assert view.language == "ro"
        assert sent.args[0] == t("help", "ro")
        assert sent.kwargs["ephemeral"] is True
        assert sent.kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
        view.stop()

    asyncio.run(run())
