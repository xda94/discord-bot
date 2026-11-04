from features.help_feature import HELP_TEXT, _chunk_text
from i18n import t


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
        HelpFeature(client, tree)
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


def test_help_text_chunks_stay_within_discord_limit():
    assert all(len(chunk) <= 1900 for chunk in _chunk_text(HELP_TEXT))


def test_help_text_lists_no_memory_or_feedback_commands():
    assert "/memory-" not in HELP_TEXT
    assert "/llm-memory" not in HELP_TEXT
    assert "/llm-feedback-summary" not in HELP_TEXT
    assert "**/worth-it**" in HELP_TEXT
