import ast
import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Union, get_args, get_origin, get_type_hints
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands

import db
from analytics import AnalyticsCommandTree
from features.bingo import BingoFeature
from features.help_feature import HelpFeature, _chunk_text, build_help_text
from features.humor import HumorFeature
from features.onboarding import CATEGORIES, COMMUNITY_GUIDE, HelpView, OnboardingFeature, OnboardingView
from features.quote_museum import QuoteMuseumFeature
from features.social_games import SocialGamesFeature


ROOT = Path(__file__).resolve().parent.parent
COMMANDS = {
    "tribunal": ("acuzat", "acuzatie"),
    "sedinta": ("subiect", "optiuni"),
    "quest": (),
    "bingo": (),
    "bingo-obicei": ("text", "text-en", "id"),
    "bingo-obicei-sterge": ("id",),
    "citat": (),
    "cine-a-zis": (),
    "citat-sterge": ("id",),
    "scuza": ("motiv", "stil"),
    "merita": ("url",),
    "horoscop": ("tema",),
}


def registered_features(memory_disabled=False):
    client = discord.Client(intents=discord.Intents.none())
    tree = AnalyticsCommandTree(client)
    games = SocialGamesFeature(client, tree)
    museum = QuoteMuseumFeature(client, tree, games)
    bingo = BingoFeature(client, tree)
    humor = HumorFeature(client, tree)
    HelpFeature(client, tree, memory_disabled=memory_disabled)
    OnboardingFeature(client, tree, memory_disabled=memory_disabled)
    return client, tree, (games, museum, bingo, humor)


def interaction(user_id=1, language="en"):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id), locale=language,
        response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def sent_content(event):
    calls = list(event.response.send_message.await_args_list) + list(event.followup.send.await_args_list)
    if event.response.edit_message.await_args:
        return [event.response.edit_message.await_args.kwargs["content"]] + [call.args[0] for call in calls]
    return [call.args[0] for call in calls]


def layout(current):
    rows = current.to_components()
    assert len(rows) <= 5
    assert all(len(row["components"]) <= 5 for row in rows)
    assert all(child.row is None or 0 <= child.row <= 4 for child in current.children)


def test_real_command_payloads_and_constructor_dependencies(tmp_db, monkeypatch):
    import features.humor as humor_module
    import features.social_games as games_module
    import llm.client as humor_llm

    private_generation = AsyncMock(side_effect=AssertionError("No model calls during registration"))
    shared_generation = AsyncMock(side_effect=AssertionError("No model calls during registration"))
    query = Mock(side_effect=AssertionError("No model calls during registration"))
    monkeypatch.setattr(humor_module, "generate_humor", private_generation)
    monkeypatch.setattr(games_module, "generate_humor", shared_generation)
    monkeypatch.setattr(humor_llm, "query_llm", query)
    client, tree, features = registered_features()
    games, museum, bingo, humor = features
    assert museum.games is games
    assert all(feature.client is client and feature.tree is tree for feature in features)
    for name, options in COMMANDS.items():
        command = tree.get_command(name)
        assert isinstance(command, app_commands.Command)
        payload = command.to_dict(tree)
        assert payload["name"] == name
        assert tuple(option["name"] for option in payload.get("options", [])) == options
        assert all("_" not in option["name"] for option in payload.get("options", []))
        assert command.guild_only is (name not in {"scuza", "merita"})
        assert len(payload["description"]) <= 100
    contexts = [command for command in tree.get_commands() if isinstance(command, app_commands.ContextMenu)]
    assert len(contexts) == 1
    assert contexts[0].to_dict(tree)["name"] == "Pune în muzeu"
    assert contexts[0].type == discord.AppCommandType.message and contexts[0].guild_only
    private_generation.assert_not_called()
    shared_generation.assert_not_called()
    query.assert_not_called()
    assert not games._restored and not museum._restored
    assert not hasattr(bingo, "start_tasks") and not hasattr(humor, "start_tasks")


def test_optional_arguments_are_typing_optional_and_payloads_match(tmp_db):
    _, tree, _ = registered_features()
    for name, parameter in (("bingo-obicei", "id"), ("horoscop", "tema")):
        command = tree.get_command(name)
        annotation = get_type_hints(command.callback)[parameter]
        assert get_origin(annotation) is Union
        assert type(None) in get_args(annotation)
        assert inspect.signature(command.callback).parameters[parameter].default is None
        option = next(option for option in command.to_dict(tree)["options"] if option["name"] == parameter)
        assert option.get("required", False) is False
    for path in ("features/social_games.py", "features/bingo.py", "features/quote_museum.py", "features/humor.py"):
        module = ast.parse((ROOT / path).read_text(), feature_version=(3, 9))
        for node in ast.walk(module):
            if isinstance(node, ast.AsyncFunctionDef) and any(isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute) and item.func.attr == "command" for item in node.decorator_list):
                assert not any(isinstance(part, ast.BinOp) and isinstance(part.op, ast.BitOr) for argument in node.args.args if argument.annotation for part in ast.walk(argument.annotation))


@pytest.mark.parametrize("name", ["help", "start"])
@pytest.mark.parametrize("language", ["en", "ro"])
@pytest.mark.parametrize("memory_disabled", [False, True])
def test_help_and_start_discover_community_with_profile_language(tmp_db, name, language, memory_disabled):
    db.set_assistant_profile(1, language=language)
    async def run():
        _, tree, _ = registered_features(memory_disabled)
        event = interaction(language="ro" if language == "en" else "en")
        await tree.get_command(name).callback(event)
        args = event.response.send_message.await_args
        assert args.kwargs["ephemeral"]
        current = args.kwargs["view"]
        assert current.language == language
        if name == "start":
            await current._help(event)
            discovered = event.response.edit_message.await_args.kwargs["view"]
            assert any(child.row == 4 for child in discovered.children)
        else:
            discovered = current
        layout(discovered)
        assert CATEGORIES["community"][language == "ro"] in [child.label for child in discovered.children]
        full_label = "Toate comenzile" if language == "ro" else "Complete reference"
        full_event = interaction(language=language)
        await next(child for child in discovered.children if child.label == full_label).callback(full_event)
        assert "Pune în muzeu" in "\n".join(sent_content(full_event))
        assert "/Pune în muzeu" not in "\n".join(sent_content(full_event))
        current.stop()
        discovered.stop()
    asyncio.run(run())


def test_bot_wires_only_background_owners_without_changing_message_dispatch():
    module = ast.parse((ROOT / "bot.py").read_text())
    assignments = {node.targets[0].id: node.value for node in module.body if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
    constructors = {"games": ("social_games", "SocialGamesFeature"), "museum": ("quote_museum", "QuoteMuseumFeature"), "bingo": ("bingo", "BingoFeature"), "humor": ("humor", "HumorFeature")}
    for variable, constructor in constructors.values():
        call = assignments[variable]
        assert isinstance(call, ast.Call) and call.func.id == constructor
        assert [argument.id for argument in call.args[:2]] == ["client", "tree"]
        assert sum(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == constructor for node in ast.walk(module)) == 1
    assert assignments["quote_museum"].args[2].id == "social_games"
    body_positions = {node.targets[0].id: index for index, node in enumerate(module.body) if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
    assert body_positions["social_games"] < body_positions["quote_museum"]
    backgrounds = [node.id for node in assignments["BACKGROUND_FEATURES"].elts]
    assert backgrounds == ["sponsors", "inactivity", "reminders", "notifications", "natural_commands", "jokes", "birthdays", "wishlist", "flights", "llm_mention", "social_games", "quote_museum"]
    handlers = assignments["MESSAGE_HANDLERS"]
    assert [node.id for node in handlers.body.elts] == ["inactivity", "user_memory", "natural_commands", "llm_mention", "keywords", "context_reactions", "teases"]
    assert [node.id for node in handlers.orelse.elts] == ["inactivity", "natural_commands", "llm_mention", "keywords", "context_reactions", "teases"]
    reaction = next(node for node in module.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_raw_reaction_add")
    assert len(reaction.body) == 1
    assert ast.unparse(reaction.body[0]) == "await llm_feedback.handle_raw_reaction_add(payload)"
    on_message = next(node for node in module.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_message")
    assert len(on_message.body) == 3
    assert ast.unparse(on_message.body[1]) == "await record_message_mention(message, BOT_ID)"
    assert ast.unparse(on_message.body[2].iter) == "MESSAGE_HANDLERS"


@pytest.mark.parametrize("language", ["en", "ro"])
@pytest.mark.parametrize("memory_disabled", [False, True])
def test_complete_reference_handles_real_message_context_menu(tmp_db, language, memory_disabled):
    async def run():
        _, tree, _ = registered_features(memory_disabled)
        current = HelpView(1, language, tree, memory_disabled=memory_disabled)
        event = interaction(language=language)
        label = "Toate comenzile" if language == "ro" else "Complete reference"
        await next(child for child in current.children if child.label == label).callback(event)
        chunks = sent_content(event)
        content = "\n".join(chunks)
        assert all(len(chunk) <= 1900 for chunk in chunks)
        for name in COMMANDS:
            assert f"**/{name}**" in content
        assert "Pune în muzeu" in content and "/Pune în muzeu" not in content
        assert ("Mesaje → Aplicații" if language == "ro" else "Messages → Apps") in content
        assert "24" in content
        assert all(call.kwargs["ephemeral"] for call in event.response.send_message.await_args_list + event.followup.send.await_args_list)
        current.stop()
    asyncio.run(run())


@pytest.mark.parametrize("language", ["en", "ro"])
@pytest.mark.parametrize("memory_disabled", [False, True])
@pytest.mark.parametrize("edit_message", [False, True])
def test_bilingual_community_guide_and_onboarding_return_layout(tmp_db, language, memory_disabled, edit_message):
    async def run():
        _, tree, _ = registered_features(memory_disabled)
        current = HelpView(1, language, tree, memory_disabled=memory_disabled, edit_message=edit_message)
        setup = OnboardingView(1, language, tree, memory_disabled=memory_disabled)
        if edit_message:
            setup._return_button(current)
            assert any(child.row == 4 for child in current.children)
        layout(current)
        category_label = CATEGORIES["community"][language == "ro"]
        category = next(child for child in current.children if child.label == category_label)
        event = interaction(language=language)
        await category.callback(event)
        chunks = sent_content(event)
        content = "\n\n".join(chunks)
        assert content == COMMUNITY_GUIDE[language]
        assert all(len(chunk) <= 1900 for chunk in chunks)
        assert all(len(paragraph) <= 1900 for paragraph in COMMUNITY_GUIDE[language].split("\n\n"))
        assert all(f"/{name}" in content for name in COMMANDS)
        assert "Pune în muzeu" in content and "600" in content and "9–50" in content
        assert "30" in content and "24" in content
        if edit_message:
            event.response.send_message.assert_not_awaited()
            assert event.response.edit_message.await_args.kwargs["view"] is current
            assert event.response.edit_message.await_args.kwargs["allowed_mentions"].everyone is False
            assert event.response.edit_message.await_args.kwargs["suppress_embeds"] is True
        else:
            event.response.edit_message.assert_not_awaited()
        for call in event.response.send_message.await_args_list + event.followup.send.await_args_list:
            assert call.kwargs["ephemeral"] and call.kwargs["suppress_embeds"]
            assert call.kwargs["allowed_mentions"].everyone is False
        wrong_user = interaction(user_id=2, language=language)
        await category.callback(wrong_user)
        wrong_user.followup.send.assert_not_awaited()
        assert sent_content(wrong_user) != chunks
        current.stop()
        setup.stop()
    asyncio.run(run())


@pytest.mark.parametrize("automatic,disabled", [(True, False), (False, False), (False, True)])
def test_static_reference_keeps_all_community_features_in_each_memory_mode(automatic, disabled):
    text = build_help_text(automatic, disabled)
    assert all(f"**/{name}**" in text for name in COMMANDS)
    assert "Messages → Apps → Pune în muzeu" in text
    assert "/Pune în muzeu" not in text
    assert "natural-language mention commands" in text
    assert "historical currency is unknown" in text
    assert all(len(chunk) <= 1900 for chunk in _chunk_text(text))
