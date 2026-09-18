import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
from discord import app_commands

import db
from features.user_memory import (
    MEMORY_SYNTHESIS_INTERVAL_SECONDS,
    SYNTHESIS_CHUNK_MAX_CHARS,
    SYNTHESIS_CHUNK_MAX_MESSAGES,
    UserMemoryFeature,
)


def _build_feature():
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    return client, tree, UserMemoryFeature(client, tree)


def _message(*, user_id=7, guild_id=100, channel_id=10, content="hello"):
    guild = SimpleNamespace(id=guild_id) if guild_id is not None else None
    return SimpleNamespace(
        author=SimpleNamespace(id=user_id),
        guild=guild,
        channel=SimpleNamespace(id=channel_id),
        clean_content=content,
    )


def test_capture_requires_enabled_channel_and_keeps_requester_scope(tmp_db):
    db.set_llm_memory_channel_enabled(100, 10, True)
    client, _, feature = _build_feature()
    enabled = _message(content="I like mechanical keyboards")
    disabled = _message(channel_id=11, content="private note")

    asyncio.run(feature.handle_message(enabled))
    asyncio.run(feature.handle_message(disabled))

    context = feature.context_for(enabled)
    assert context.enabled is True
    assert context.batch is not None
    assert context.batch.observations == ("I like mechanical keyboards",)
    assert feature.context_for(disabled).enabled is False
    asyncio.run(client.close())


def test_daily_synthesis_chunks_keep_every_pending_message(tmp_db):
    db.set_llm_memory_channel_enabled(100, 10, True)
    client, _, feature = _build_feature()
    message = _message()
    for index in range(130):
        message.clean_content = f"{index}:" + ("x" * 120)
        asyncio.run(feature.handle_message(message))

    batch = feature.context_for(message).batch
    assert batch is not None
    assert len(batch.observations) == 130
    assert batch.observations[-1].startswith("129:")
    chunks = feature.synthesis_chunks(batch)
    assert sum(len(chunk.observations) for chunk in chunks) == 130
    assert tuple(
        text for chunk in chunks for text in chunk.observations
    ) == batch.observations
    assert all(
        len(chunk.observations) <= SYNTHESIS_CHUNK_MAX_MESSAGES
        and sum(len(text) for text in chunk.observations)
        <= SYNTHESIS_CHUNK_MAX_CHARS
        for chunk in chunks
    )
    asyncio.run(client.close())


def test_daily_synthesis_waits_24_hours_and_deletes_source_chat(tmp_db):
    db.set_llm_memory_channel_enabled(100, 10, True)
    client, _, feature = _build_feature()
    message = _message(content="I really like mechanical keyboards")
    asyncio.run(feature.handle_message(message))
    created_at = db.get_llm_memory_observations(100, 7)[0][-1]

    assert feature.eligible_batches(
        now=created_at + MEMORY_SYNTHESIS_INTERVAL_SECONDS - 1
    ) == []
    batches = feature.eligible_batches(
        now=created_at + MEMORY_SYNTHESIS_INTERVAL_SECONDS
    )
    assert len(batches) == 1

    assert feature.commit_delta(
        batches[0],
        (
            {
                "kind": "like",
                "content": "Likes mechanical keyboards",
                "source_text": "I really like mechanical keyboards",
            },
        ),
        (),
    )
    assert db.get_llm_memory_observations(100, 7) == []
    assert db.get_llm_memory_transcript(100, 7) == []
    assert [(row[1], row[2]) for row in db.get_llm_memory_entries(100, 7)] == [
        ("like", "Likes mechanical keyboards")
    ]
    asyncio.run(client.close())


def test_large_memory_snapshot_uses_small_requests_without_losing_sources(tmp_db):
    db.set_llm_memory_channel_enabled(100, 10, True)
    additions = tuple(
        {"kind": "fact", "content": str(index) + "x" * 480, "source_text": "source"}
        for index in range(20)
    )
    assert db.apply_llm_memory_delta(100, 7, additions, (), ())
    client, _, feature = _build_feature()
    message = _message()
    for index in range(20):
        message.clean_content = str(index) + "y" * 990
        asyncio.run(feature.handle_message(message))

    snapshot = feature.context_for(message).batch
    chunks = feature.synthesis_chunks(snapshot)
    assert len(chunks) > 1
    for chunk in chunks:
        entries = feature.entries_for_batch(chunk)
        input_chars = sum(len(text) for text in chunk.observations)
        input_chars += sum(len(entry["content"]) for entry in entries)
        assert input_chars <= 5000
        assert feature.commit_delta(chunk, (), ())
    assert not db.get_llm_memory_observations(100, 7)
    assert (100, 7) not in feature._buffers
    assert len(db.get_llm_memory_entries(100, 7)) == 20
    asyncio.run(client.close())


def test_new_chat_after_synthesis_starts_a_new_daily_window(tmp_db):
    db.set_llm_memory_channel_enabled(100, 10, True)
    client, _, feature = _build_feature()
    message = _message(content="first day")
    asyncio.run(feature.handle_message(message))
    first_created = db.get_llm_memory_observations(100, 7)[0][-1]
    batch = feature.eligible_batches(
        now=first_created + MEMORY_SYNTHESIS_INTERVAL_SECONDS
    )[0]
    assert feature.commit_delta(batch, (), ())

    message.clean_content = "second day"
    asyncio.run(feature.handle_message(message))
    second_created = db.get_llm_memory_observations(100, 7)[0][-1]
    assert feature.eligible_batches(
        now=second_created + MEMORY_SYNTHESIS_INTERVAL_SECONDS - 1
    ) == []
    assert len(
        feature.eligible_batches(
            now=second_created + MEMORY_SYNTHESIS_INTERVAL_SECONDS
        )
    ) == 1
    asyncio.run(client.close())


def test_commit_acknowledges_only_snapshot_and_persists_entry(tmp_db):
    db.set_llm_memory_channel_enabled(100, 10, True)
    client, _, feature = _build_feature()
    message = _message(content="first")
    asyncio.run(feature.handle_message(message))
    batch = feature.context_for(message).batch
    assert batch is not None

    message.clean_content = "arrived while summarizing"
    asyncio.run(feature.handle_message(message))
    assert feature.commit_delta(
        batch,
        (
            {
                "kind": "fact",
                "content": "Likes mechanical keyboards",
                "source_text": "first",
            },
        ),
        (),
    )

    saved = db.get_llm_memory_entries(100, 7)
    assert [row[2] for row in saved] == ["Likes mechanical keyboards"]
    remaining = feature.context_for(message).batch
    assert remaining is not None
    assert remaining.observations == ("arrived while summarizing",)
    asyncio.run(client.close())


def test_commit_corrects_only_targeted_saved_fact(tmp_db):
    db.set_llm_memory_channel_enabled(100, 10, True)
    db.apply_llm_memory_delta(
        100,
        7,
        (
            {"kind": "fact", "content": "Prefers Python", "source_text": "old"},
            {"kind": "fact", "content": "Owns a cat", "source_text": "cat"},
        ),
        (),
        (),
    )
    client, _, feature = _build_feature()
    message = _message(content="I prefer Rust now")
    asyncio.run(feature.handle_message(message))
    batch = feature.context_for(message).batch
    assert batch is not None
    rows = db.get_llm_memory_entries(100, 7)
    target_id = next(row[0] for row in rows if row[2] == "Prefers Python")
    assert feature.commit_delta(
        batch,
        (),
        (
            {
                "id": target_id,
                "kind": "fact",
                "content": "Prefers Rust",
                "source_text": "I prefer Rust now",
            },
        ),
    )
    updated = db.get_llm_memory_entries(100, 7)
    assert {row[2] for row in updated} == {"Prefers Rust", "Owns a cat"}
    assert any(row[0] == target_id and row[2] == "Prefers Rust" for row in updated)
    assert feature.context_for(message).batch is None
    asyncio.run(client.close())


def test_invalidation_prevents_in_flight_profile_recreation(tmp_db):
    db.set_llm_memory_channel_enabled(100, 10, True)
    client, _, feature = _build_feature()
    message = _message(content="remember this")
    asyncio.run(feature.handle_message(message))
    batch = feature.context_for(message).batch
    assert batch is not None

    feature._purge_buffers(100)

    assert feature.commit_delta(
        batch,
        ({"kind": "fact", "content": "Something", "source_text": "remember"},),
        (),
    ) is False
    assert db.get_llm_memory_entries(100, 7) == []
    asyncio.run(client.close())


def test_external_memory_deletion_invalidates_process_local_batch(tmp_db):
    db.set_llm_memory_channel_enabled(100, 10, True)
    client, _, feature = _build_feature()
    message = _message(content="remember this from the dashboard")
    asyncio.run(feature.handle_message(message))
    batch = feature.context_for(message).batch
    assert batch is not None

    assert db.delete_all_llm_user_memory(100, 7) > 0

    assert feature.can_process_batch(batch) is False
    assert feature.context_for(message).batch is None
    asyncio.run(client.close())


def test_dm_memory_requires_explicit_opt_in(tmp_db):
    client, _, feature = _build_feature()
    message = _message(guild_id=None, channel_id=99, content="DM fact")
    asyncio.run(feature.handle_message(message))
    assert feature.context_for(message).enabled is False

    db.set_llm_memory_preference(0, 7, True)
    feature._preference_cache.clear()
    asyncio.run(feature.handle_message(message))
    assert feature.context_for(message).batch.observations == ("DM fact",)
    asyncio.run(client.close())


def test_llm_memory_activation_is_public_and_channel_scoped(tmp_db):
    client, tree, feature = _build_feature()
    interaction = MagicMock()
    interaction.guild_id = 100
    interaction.channel_id = 10
    interaction.user.guild_permissions.manage_guild = True
    interaction.user.guild_permissions.administrator = False
    interaction.response.send_message = AsyncMock()

    asyncio.run(tree.get_command("llm-memory").callback(interaction, "activate"))

    assert db.is_llm_memory_channel_enabled(100, 10) is True
    sent = interaction.response.send_message.await_args
    assert "enabled in this channel" in sent.args[0]
    assert "ephemeral" not in sent.kwargs
    assert (100, 10) in feature._enabled_channels
    asyncio.run(client.close())


def test_llm_memory_command_rejects_dm_and_missing_permission(tmp_db):
    client, tree, _ = _build_feature()
    command = tree.get_command("llm-memory")

    dm_interaction = MagicMock()
    dm_interaction.guild_id = None
    dm_interaction.channel_id = 10
    dm_interaction.response.send_message = AsyncMock()
    asyncio.run(command.callback(dm_interaction, "activate"))
    assert dm_interaction.response.send_message.await_args.kwargs["ephemeral"] is True

    interaction = MagicMock()
    interaction.guild_id = 100
    interaction.channel_id = 10
    interaction.user.guild_permissions.manage_guild = False
    interaction.user.guild_permissions.administrator = False
    interaction.response.send_message = AsyncMock()
    asyncio.run(command.callback(interaction, "activate"))
    assert db.is_llm_memory_channel_enabled(100, 10) is False
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True
    asyncio.run(client.close())


def test_channel_deactivation_discards_only_that_channels_observations(tmp_db):
    db.set_llm_memory_channel_enabled(100, 10, True)
    db.set_llm_memory_channel_enabled(100, 11, True)
    db.set_llm_user_memory(100, 7, "Saved profile")
    client, tree, feature = _build_feature()
    first = _message(channel_id=10, content="first channel")
    second = _message(channel_id=11, content="second channel")
    asyncio.run(feature.handle_message(first))
    asyncio.run(feature.handle_message(second))
    interaction = MagicMock()
    interaction.guild_id = 100
    interaction.channel_id = 10
    interaction.user.guild_permissions.manage_guild = True
    interaction.user.guild_permissions.administrator = False
    interaction.response.send_message = AsyncMock()

    asyncio.run(tree.get_command("llm-memory").callback(interaction, "deactivate"))

    remaining = feature.context_for(second)
    assert remaining.batch is not None
    assert remaining.batch.observations == ("second channel",)
    assert db.get_llm_user_memory(100, 7) == "Saved profile"
    assert feature.context_for(first).enabled is False
    asyncio.run(client.close())


def test_user_opt_out_erases_profile_and_buffer(tmp_db):
    db.set_llm_memory_channel_enabled(100, 10, True)
    db.set_llm_user_memory(100, 7, "Saved profile")
    client, tree, feature = _build_feature()
    message = _message(content="pending")
    asyncio.run(feature.handle_message(message))
    interaction = MagicMock()
    interaction.guild_id = 100
    interaction.user.id = 7
    interaction.response.send_message = AsyncMock()

    asyncio.run(tree.get_command("memory-opt-out").callback(interaction))

    assert db.get_llm_memory_preference(100, 7) is False
    assert db.get_llm_user_memory(100, 7) is None
    assert feature.context_for(message).enabled is False
    interaction.response.send_message.assert_awaited_once()
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True
    asyncio.run(client.close())


def test_server_purge_requires_exact_confirmation_and_preserves_opt_out(tmp_db):
    db.set_llm_user_memory(100, 7, "Saved profile")
    db.set_llm_memory_preference(100, 8, False)
    client, tree, _ = _build_feature()
    interaction = MagicMock()
    interaction.guild_id = 100
    interaction.user.guild_permissions.manage_guild = True
    interaction.user.guild_permissions.administrator = False
    interaction.response.send_message = AsyncMock()

    command = tree.get_command("llm-memory-purge")
    asyncio.run(command.callback(interaction, "purge"))
    assert db.get_llm_user_memory(100, 7) == "Saved profile"

    interaction.response.send_message.reset_mock()
    asyncio.run(command.callback(interaction, "PURGE"))
    assert db.get_llm_user_memory(100, 7) is None
    assert db.get_llm_memory_preference(100, 8) is False
    asyncio.run(client.close())


def test_memory_show_chunks_larger_profile_privately(tmp_db):
    profile = "x" * 3900
    db.set_llm_user_memory(100, 7, profile)
    client, tree, _ = _build_feature()
    interaction = MagicMock()
    interaction.guild_id = 100
    interaction.user.id = 7
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()

    asyncio.run(tree.get_command("memory-show").callback(interaction))

    first = interaction.response.send_message.await_args
    followups = interaction.followup.send.await_args_list
    chunks = [first.args[0]] + [call.args[0] for call in followups]
    assert len(chunks) == 3
    assert "".join(chunks) == f"**What I remember about you**\n{profile}"
    assert first.kwargs["ephemeral"] is True
    assert all(call.kwargs["ephemeral"] is True for call in followups)
    asyncio.run(client.close())
