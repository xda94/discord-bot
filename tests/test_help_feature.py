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


def test_legacy_help_points_to_natural_language_guide_in_every_memory_mode():
    for automatic, disabled in ((True, False), (False, False), (False, True)):
        text = build_help_text(automatic, disabled)
        assert "**/natural-language**" in text
        assert "Calendar/recurring reminders require a confirmed timezone" in text
        assert "clarifications last ten minutes" in text


def test_natural_language_registration_is_private_read_only_and_has_no_arguments(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import discord
    from discord import app_commands
    from features.help_feature import HelpFeature
    from features.natural_language_help import NaturalLanguageHelpView

    reads = []

    def read(user_id):
        reads.append(user_id)
        return {"language": "ro"}

    def forbid_write(*args, **kwargs):
        raise AssertionError("The guide must be read-only")

    monkeypatch.setattr("db.get_assistant_profile", read)
    monkeypatch.setattr("db.set_assistant_profile", forbid_write)

    async def run():
        for automatic, disabled in ((True, False), (False, False), (False, True)):
            client = discord.Client(intents=discord.Intents.none())
            client._connection.user = SimpleNamespace(mention="<@999888777>")
            tree = app_commands.CommandTree(client)
            HelpFeature(client, tree, automatic_memory_enabled=automatic, memory_disabled=disabled)
            command = tree.get_command("natural-language")
            assert command is not None and command.parameters == []
            assert tree.get_command("help") is not None
            interaction = SimpleNamespace(user=SimpleNamespace(id=10), locale=discord.Locale.american_english,
                                          response=SimpleNamespace(send_message=AsyncMock()))
            await command.callback(interaction)
            interaction.response.send_message.assert_awaited_once()
            sent = interaction.response.send_message.call_args
            assert sent.kwargs["ephemeral"] is True
            assert "<@999888777>" in sent.args[0]
            assert isinstance(sent.kwargs["view"], NaturalLanguageHelpView)
            assert sent.kwargs["view"].language == "ro"
            assert sent.kwargs["view"].user_id == 10
            assert sent.kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
            sent.kwargs["view"].stop()
        assert reads == [10, 10, 10]

    asyncio.run(run())


def test_help_uses_locale_and_preserves_real_mention_for_guide_link(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import discord
    from discord import app_commands
    from features.help_feature import HelpFeature
    from features.natural_language_help import NaturalLanguageHelpView

    monkeypatch.setattr("db.get_assistant_profile", lambda user_id: {"language": "auto"})

    async def run():
        client = discord.Client(intents=discord.Intents.none())
        client._connection.user = SimpleNamespace(mention="<@999888777>")
        tree = app_commands.CommandTree(client)
        HelpFeature(client, tree, memory_disabled=True)
        interaction = SimpleNamespace(user=SimpleNamespace(id=10), locale=discord.Locale.romanian,
                                      response=SimpleNamespace(send_message=AsyncMock()))
        await tree.get_command("help").callback(interaction)
        sent = interaction.response.send_message.call_args
        view = sent.kwargs["view"]
        assert view.language == "ro"
        assert sent.kwargs["ephemeral"] is True
        assert sent.kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
        # The natural-language link remains available when all memory is disabled.
        button = next(child for child in view.children if getattr(child, "label", None) == t("natural_examples", "ro"))
        interaction.response.send_message.reset_mock()
        await button.callback(interaction)
        sent = interaction.response.send_message.call_args
        assert "<@999888777>" in sent.args[0]
        assert isinstance(sent.kwargs["view"], NaturalLanguageHelpView)
        sent.kwargs["view"].stop()
        view.stop()

    asyncio.run(run())
