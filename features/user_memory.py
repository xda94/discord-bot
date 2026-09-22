"""Discord commands and event adapter for persistent user memory."""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import discord
from discord import app_commands

import db
from llm.memory import (
    MANUAL_MEMORY_ENTRY_MAX_CHARS,
    MANUAL_MEMORY_TYPES,
    MEMORY_KIND_LABELS,
    PRIVATE_MESSAGE_CHUNK,
    SYNTHESIS_CHUNK_MAX_CHARS,
    SYNTHESIS_CHUNK_MAX_MESSAGES,
    MemoryBatch,
    MemoryContext,
    MemoryObservation,
    MemoryStore,
    _scope_id,
)

logger = logging.getLogger("discord_bot")


def is_automatic_memory_enabled() -> bool:
    """Return whether automatic capture and synthesis are enabled."""
    raw_value = os.getenv("LLM_MEMORY_ENABLED")
    if raw_value is None:
        return False
    if raw_value not in {"0", "1"}:
        logger.warning(
            "LLM_MEMORY_ENABLED must be 0 or 1; using manual memory mode for %r.",
            raw_value,
        )
        return False
    return raw_value == "1"


async def _send_private(interaction: discord.Interaction, text: str) -> None:
    chunks = [
        text[index:index + PRIVATE_MESSAGE_CHUNK]
        for index in range(0, len(text), PRIVATE_MESSAGE_CHUNK)
    ] or [""]
    await interaction.response.send_message(chunks[0], ephemeral=True)
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=True)


class UserMemoryFeature:
    """Register memory commands and adapt Discord messages to MemoryStore."""

    def __init__(
        self,
        client: discord.Client,
        tree: app_commands.CommandTree,
        *,
        automatic_enabled: bool = True,
    ):
        self.client = client
        self.tree = tree
        self.automatic_enabled = automatic_enabled
        self.store = MemoryStore(automatic_enabled=automatic_enabled)
        self._register_commands()

    async def handle_message(self, message: discord.Message) -> bool:
        return self.store.capture_message(
            user_id=message.author.id,
            guild_id=message.guild.id if message.guild is not None else None,
            channel_id=message.channel.id,
            content=message.clean_content,
        )

    def context_for(self, message: discord.Message) -> MemoryContext:
        return self.store.context_for(
            user_id=message.author.id,
            guild_id=message.guild.id if message.guild is not None else None,
            channel_id=message.channel.id,
            query=getattr(message, "clean_content", ""),
        )

    def commit_delta(self, batch: MemoryBatch, additions, corrections) -> bool:
        return self.store.commit_delta(batch, additions, corrections)

    def entries_for_batch(self, batch: MemoryBatch) -> list[dict]:
        return self.store.entries_for_batch(batch)

    @staticmethod
    def synthesis_chunks(batch: MemoryBatch) -> tuple[MemoryBatch, ...]:
        return MemoryStore.synthesis_chunks(batch)

    def eligible_batches(self, **kwargs) -> list[MemoryBatch]:
        return self.store.eligible_batches(**kwargs)

    def can_process_batch(self, batch: MemoryBatch) -> bool:
        return self.store.can_process_batch(batch)

    @staticmethod
    def _can_manage_server(interaction: discord.Interaction) -> bool:
        permissions = getattr(interaction.user, "guild_permissions", None)
        return bool(
            permissions
            and (permissions.manage_guild or permissions.administrator)
        )

    def _register_manual_commands(self) -> None:
        @self.tree.command(
            name="memory-add",
            description="Add something the bot should remember about you",
        )
        @app_commands.rename(memory_type="type")
        @app_commands.describe(
            memory_type="What kind of memory this is",
            text="The complete memory to remember",
        )
        @app_commands.choices(
            memory_type=[
                app_commands.Choice(name=label, value=value)
                for value, label in MANUAL_MEMORY_TYPES.items()
            ]
        )
        async def memory_add(
            interaction: discord.Interaction, memory_type: str, text: str
        ):
            content = " ".join(text.split())
            if memory_type not in MANUAL_MEMORY_TYPES:
                await interaction.response.send_message(
                    "Choose one of the available memory types.", ephemeral=True
                )
                return
            if not content:
                await interaction.response.send_message(
                    "Memory text cannot be empty.", ephemeral=True
                )
                return
            if len(content) > MANUAL_MEMORY_ENTRY_MAX_CHARS:
                await interaction.response.send_message(
                    f"Memory text must be at most {MANUAL_MEMORY_ENTRY_MAX_CHARS} "
                    "characters.",
                    ephemeral=True,
                )
                return

            scope_id = _scope_id(interaction.guild_id)
            user_id = interaction.user.id
            if not db.set_llm_memory_preference(scope_id, user_id, True):
                await interaction.response.send_message(
                    "Could not enable memory for you. Please try again.",
                    ephemeral=True,
                )
                return
            self.store._preference_cache[(scope_id, user_id)] = True
            self.store._preference_checked_at[(scope_id, user_id)] = time.monotonic()

            inserted = db.add_manual_llm_memory_entry(
                scope_id,
                user_id,
                content,
                kind=memory_type,
            )
            if inserted is None:
                await interaction.response.send_message(
                    "Could not save that memory. Please try again.", ephemeral=True
                )
            elif inserted is False:
                await interaction.response.send_message(
                    "I already remember that about you here.", ephemeral=True
                )
            else:
                label = MANUAL_MEMORY_TYPES[memory_type]
                await interaction.response.send_message(
                    f"🧠 Saved under **{label}**: **{content}**",
                    ephemeral=True,
                )

        @self.tree.command(
            name="memory-show",
            description="Privately show what the bot remembers about you",
        )
        async def memory_show(interaction: discord.Interaction):
            scope_id = _scope_id(interaction.guild_id)
            entries = db.get_llm_memory_entries(scope_id, interaction.user.id)
            profile = db.get_llm_user_memory(scope_id, interaction.user.id)
            if not entries and not profile:
                await _send_private(
                    interaction, "The bot does not remember anything about you here yet."
                )
                return

            sections = ["**What I remember about you**"]
            if entries:
                grouped: dict[str, list[str]] = {}
                for _entry_id, kind, content, _source, _created, _updated in entries:
                    grouped.setdefault(kind, []).append(content)
                for kind in MEMORY_KIND_LABELS:
                    memories = grouped.get(kind)
                    if memories:
                        sections.append(
                            f"**{MEMORY_KIND_LABELS[kind]}**\n"
                            + "\n".join(f"- {content}" for content in memories)
                        )
            if profile:
                sections.append(f"**Legacy profile**\n{profile}")
            await _send_private(interaction, "\n".join(sections))

        @self.tree.command(
            name="memory-erase",
            description="Erase one pasted memory, or all of your memory here",
        )
        @app_commands.describe(
            text="Paste the complete memory text, or omit this to erase everything"
        )
        async def memory_erase(
            interaction: discord.Interaction, text: Optional[str] = None
        ):
            scope_id = _scope_id(interaction.guild_id)
            user_id = interaction.user.id
            if text is not None:
                removed = db.delete_llm_memory_entries_by_text(
                    scope_id, user_id, text
                )
                if removed is None:
                    await interaction.response.send_message(
                        "Could not erase that memory. Please try again.",
                        ephemeral=True,
                    )
                elif removed == 0:
                    await interaction.response.send_message(
                        "I could not find that exact memory about you here.",
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        f"Erased **{removed}** matching memory entr"
                        f"{'y' if removed == 1 else 'ies'}.",
                        ephemeral=True,
                    )
                return

            self.store._invalidate_key((scope_id, user_id), clear=True)
            removed = db.delete_all_llm_user_memory(scope_id, user_id)
            if removed is None:
                await interaction.response.send_message(
                    "Could not erase all of your memory. Please try again.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                "Your saved memory and pending observations were erased here.",
                ephemeral=True,
            )

    def _register_commands(self) -> None:
        if not self.automatic_enabled:
            self._register_manual_commands()
            return

        @self.tree.command(
            name="llm-memory",
            description="Configure persistent LLM memory for this channel",
        )
        @app_commands.describe(action="Activate, deactivate, or inspect this channel")
        @app_commands.choices(
            action=[
                app_commands.Choice(name="Activate", value="activate"),
                app_commands.Choice(name="Deactivate", value="deactivate"),
                app_commands.Choice(name="Status", value="status"),
            ]
        )
        @app_commands.default_permissions(manage_guild=True)
        async def llm_memory(interaction: discord.Interaction, action: str):
            if interaction.guild_id is None or interaction.channel_id is None:
                await interaction.response.send_message(
                    "This command must be run inside a server channel.",
                    ephemeral=True,
                )
                return
            if not self._can_manage_server(interaction):
                await interaction.response.send_message(
                    "You need the **Manage Server** permission to change this setting.",
                    ephemeral=True,
                )
                return

            pair = (interaction.guild_id, interaction.channel_id)
            if action == "status":
                self._refresh_enabled_channels()
                state = "enabled" if pair in self.store._enabled_channels else "disabled"
                await interaction.response.send_message(
                    f"Persistent LLM memory is **{state}** in this channel.",
                    ephemeral=True,
                )
                return

            enabled = action == "activate"
            if not db.set_llm_memory_channel_enabled(
                interaction.guild_id, interaction.channel_id, enabled
            ):
                await interaction.response.send_message(
                    "Could not update the memory setting. Please try again.",
                    ephemeral=True,
                )
                return

            if enabled:
                self.store._enabled_channels.add(pair)
                text = (
                    "🧠 **Persistent bot memory is now enabled in this channel.**\n"
                    "Once this channel has 50 captured messages that are at least "
                    "10 minutes old, the bot synthesizes them into facts, "
                    "impressions, preferences, interests, opinions, and topic notes "
                    "for each member. "
                    "The source chat is then deleted and only the synthesis is used "
                    "when that same member mentions it. Use `/memory-show`, "
                    "`/memory-forget`, or `/memory-opt-out` for personal control."
                )
            else:
                self.store._enabled_channels.discard(pair)
                self.store._invalidate_channel(interaction.guild_id, interaction.channel_id)
                text = (
                    "🧠 Persistent bot memory is now **disabled** in this channel. "
                    "Existing saved memory is retained until forgotten or purged."
                )
            self.store._enabled_channels_checked_at = time.monotonic()
            await interaction.response.send_message(text)

        @self.tree.command(
            name="llm-memory-purge",
            description="Permanently delete all saved user memory for this server",
        )
        @app_commands.describe(confirmation="Type PURGE to confirm")
        @app_commands.default_permissions(manage_guild=True)
        async def llm_memory_purge(
            interaction: discord.Interaction, confirmation: str
        ):
            if interaction.guild_id is None:
                await interaction.response.send_message(
                    "This command must be run inside a server.", ephemeral=True
                )
                return
            if not self._can_manage_server(interaction):
                await interaction.response.send_message(
                    "You need the **Manage Server** permission to purge memory.",
                    ephemeral=True,
                )
                return
            if confirmation != "PURGE":
                await interaction.response.send_message(
                    "Nothing was deleted. Type `PURGE` exactly to confirm.",
                    ephemeral=True,
                )
                return

            removed = db.purge_guild_llm_user_memories(interaction.guild_id)
            if removed is None:
                await interaction.response.send_message(
                    "Could not purge saved memory. Please try again.", ephemeral=True
                )
                return
            self.store._purge_buffers(interaction.guild_id)
            await interaction.response.send_message(
                f"🧠 Purged saved memory for **{removed}** user(s) in this server. "
                "Channel settings and individual opt-outs were preserved."
            )

        @self.tree.command(
            name="memory-show", description="Privately show what the bot remembers about you"
        )
        async def memory_show(interaction: discord.Interaction):
            scope_id = _scope_id(interaction.guild_id)
            entries = db.get_llm_memory_entries(scope_id, interaction.user.id)
            profile = db.get_llm_user_memory(scope_id, interaction.user.id)
            preference = self.store._preference(scope_id, interaction.user.id)
            if preference is False:
                await _send_private(
                    interaction,
                    "You are opted out of persistent memory in this scope, and no "
                    "saved profile is being used.",
                )
            elif not entries and not profile:
                await _send_private(
                    interaction, "The bot does not have a saved profile for you here yet."
                )
            else:
                sections = ["**What I remember about you**"]
                if entries:
                    sections.append(
                        "\n".join(
                            f"- [{kind} #{entry_id}] {content}"
                            for entry_id, kind, content, _source, _created, _updated
                            in entries
                        )
                    )
                elif profile:
                    sections.append(profile)
                await _send_private(interaction, "\n".join(sections))

        @self.tree.command(
            name="memory-forget",
            description="Erase your saved profile here without opting out",
        )
        async def memory_forget(interaction: discord.Interaction):
            scope_id = _scope_id(interaction.guild_id)
            self.store._invalidate_key((scope_id, interaction.user.id), clear=True)
            removed = db.delete_all_llm_user_memory(scope_id, interaction.user.id)
            if removed is None:
                await interaction.response.send_message(
                    "Pending observations were cleared, but the saved profile could "
                    "not be deleted. Please try again.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                "Your saved profile and pending observations were erased. Future "
                "learning remains enabled wherever memory is active.",
                ephemeral=True,
            )

        @self.tree.command(
            name="memory-opt-out",
            description="Stop persistent memory and erase your profile here",
        )
        async def memory_opt_out(interaction: discord.Interaction):
            scope_id = _scope_id(interaction.guild_id)
            if not db.set_llm_memory_preference(
                scope_id, interaction.user.id, False
            ):
                await interaction.response.send_message(
                    "Could not update your preference. Please try again.", ephemeral=True
                )
                return
            self.store._preference_cache[(scope_id, interaction.user.id)] = False
            self.store._preference_checked_at[(scope_id, interaction.user.id)] = time.monotonic()
            self.store._invalidate_key((scope_id, interaction.user.id), clear=True)
            removed = db.delete_all_llm_user_memory(scope_id, interaction.user.id)
            if removed is None:
                await interaction.response.send_message(
                    "You are opted out and pending observations were cleared, but the "
                    "saved profile could not be deleted. Please try again.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                "You are opted out here. Your saved profile and pending observations "
                "were erased.",
                ephemeral=True,
            )

        @self.tree.command(
            name="memory-opt-in",
            description="Allow persistent memory for you here",
        )
        async def memory_opt_in(interaction: discord.Interaction):
            scope_id = _scope_id(interaction.guild_id)
            if not db.set_llm_memory_preference(scope_id, interaction.user.id, True):
                await interaction.response.send_message(
                    "Could not update your preference. Please try again.", ephemeral=True
                )
                return
            self.store._preference_cache[(scope_id, interaction.user.id)] = True
            self.store._preference_checked_at[(scope_id, interaction.user.id)] = time.monotonic()
            location = (
                "this DM"
                if interaction.guild_id is None
                else "memory-enabled channels in this server"
            )
            await interaction.response.send_message(
                f"Persistent memory is enabled for you in {location}.", ephemeral=True
            )
