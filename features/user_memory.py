from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass

import discord
from discord import app_commands

import db

logger = logging.getLogger("discord_bot")

DM_SCOPE_ID = 0
MEMORY_MAX_CHARS = 2000
OBSERVATION_MAX_CHARS = 2000
OBSERVATION_MAX_MESSAGES = 20
PRIVATE_MESSAGE_CHUNK = 1900


@dataclass(frozen=True)
class MemoryObservation:
    sequence: int
    channel_id: int
    content: str


@dataclass(frozen=True)
class MemoryBatch:
    scope_id: int
    user_id: int
    guild_id: int | None
    request_channel_id: int
    generation: int
    through_sequence: int
    observations: tuple[str, ...]


@dataclass(frozen=True)
class MemoryContext:
    enabled: bool
    profile: str = ""
    batch: MemoryBatch | None = None


def _scope_id(guild_id: int | None) -> int:
    return guild_id if guild_id is not None else DM_SCOPE_ID


def _trim_profile(profile: str) -> str:
    profile = profile.strip()
    if len(profile) <= MEMORY_MAX_CHARS:
        return profile
    shortened = profile[:MEMORY_MAX_CHARS]
    split_at = max(shortened.rfind("\n"), shortened.rfind(" "))
    return shortened[:split_at].rstrip() if split_at > 0 else shortened


async def _send_private(interaction: discord.Interaction, text: str) -> None:
    chunks = [
        text[index:index + PRIVATE_MESSAGE_CHUNK]
        for index in range(0, len(text), PRIVATE_MESSAGE_CHUNK)
    ] or [""]
    await interaction.response.send_message(chunks[0], ephemeral=True)
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=True)


class UserMemoryFeature:
    """Channel-controlled, per-user conversational memory.

    Discord text is held only in the process-local buffers below. SQLite owns
    the compact profile, channel configuration, and explicit user preference.
    """

    def __init__(self, client: discord.Client, tree: app_commands.CommandTree):
        self.client = client
        self.tree = tree
        self._enabled_channels: set[tuple[int, int]] = set(
            db.get_enabled_llm_memory_channels()
        )
        self._preference_cache: dict[tuple[int, int], bool | None] = {}
        self._buffers: dict[tuple[int, int], deque[MemoryObservation]] = {}
        self._generations: dict[tuple[int, int], int] = {}
        self._next_sequence = 1
        self._register_commands()

    @staticmethod
    def _key(guild_id: int | None, user_id: int) -> tuple[int, int]:
        return (_scope_id(guild_id), user_id)

    def _preference(self, scope_id: int, user_id: int) -> bool | None:
        key = (scope_id, user_id)
        if key not in self._preference_cache:
            self._preference_cache[key] = db.get_llm_memory_preference(
                scope_id, user_id
            )
        return self._preference_cache[key]

    def _is_enabled(
        self,
        *,
        guild_id: int | None,
        channel_id: int,
        user_id: int,
    ) -> bool:
        scope_id = _scope_id(guild_id)
        preference = self._preference(scope_id, user_id)
        if guild_id is None:
            # DMs have no server manager, so they require explicit user opt-in.
            return preference is True
        return (
            (guild_id, channel_id) in self._enabled_channels
            and preference is not False
        )

    def _invalidate_key(self, key: tuple[int, int], *, clear: bool) -> None:
        self._generations[key] = self._generations.get(key, 0) + 1
        if clear:
            self._buffers.pop(key, None)

    def _invalidate_channel(self, guild_id: int, channel_id: int) -> None:
        for key, observations in list(self._buffers.items()):
            if key[0] != guild_id:
                continue
            self._generations[key] = self._generations.get(key, 0) + 1
            retained = deque(
                observation
                for observation in observations
                if observation.channel_id != channel_id
            )
            if retained:
                self._buffers[key] = retained
            else:
                self._buffers.pop(key, None)

    def _purge_buffers(self, scope_id: int) -> None:
        keys = {key for key in self._buffers if key[0] == scope_id}
        keys.update(key for key in self._generations if key[0] == scope_id)
        for key in keys:
            self._invalidate_key(key, clear=True)

    def _append_observation(
        self,
        *,
        guild_id: int | None,
        channel_id: int,
        user_id: int,
        content: str,
    ) -> None:
        key = self._key(guild_id, user_id)
        observations = self._buffers.setdefault(key, deque())
        observations.append(
            MemoryObservation(
                sequence=self._next_sequence,
                channel_id=channel_id,
                content=content[:OBSERVATION_MAX_CHARS],
            )
        )
        self._next_sequence += 1

        while len(observations) > OBSERVATION_MAX_MESSAGES:
            observations.popleft()
        while (
            len(observations) > 1
            and sum(len(item.content) for item in observations)
            > OBSERVATION_MAX_CHARS
        ):
            observations.popleft()

    async def handle_message(self, message: discord.Message) -> bool:
        content = message.clean_content.strip()
        if not content:
            return False
        guild_id = message.guild.id if message.guild is not None else None
        if not self._is_enabled(
            guild_id=guild_id,
            channel_id=message.channel.id,
            user_id=message.author.id,
        ):
            return False
        self._append_observation(
            guild_id=guild_id,
            channel_id=message.channel.id,
            user_id=message.author.id,
            content=content,
        )
        return False

    def context_for(self, message: discord.Message) -> MemoryContext:
        """Snapshot the requester's current profile and pending observations."""
        guild_id = message.guild.id if message.guild is not None else None
        channel_id = message.channel.id
        user_id = message.author.id
        if not self._is_enabled(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
        ):
            return MemoryContext(enabled=False)

        scope_id = _scope_id(guild_id)
        key = (scope_id, user_id)
        profile = _trim_profile(db.get_llm_user_memory(scope_id, user_id) or "")
        observations = self._buffers.get(key)
        batch = None
        if observations:
            snapshot = tuple(observations)
            batch = MemoryBatch(
                scope_id=scope_id,
                user_id=user_id,
                guild_id=guild_id,
                request_channel_id=channel_id,
                generation=self._generations.get(key, 0),
                through_sequence=snapshot[-1].sequence,
                observations=tuple(item.content for item in snapshot),
            )
        return MemoryContext(enabled=True, profile=profile, batch=batch)

    def commit_batch(self, batch: MemoryBatch, profile: str | None) -> bool:
        """Persist and acknowledge a successfully summarized buffer snapshot."""
        if not self.can_process_batch(batch):
            return False

        key = (batch.scope_id, batch.user_id)
        if profile is not None:
            normalized = _trim_profile(profile)
            if not normalized or not db.set_llm_user_memory(
                batch.scope_id, batch.user_id, normalized
            ):
                return False

        observations = self._buffers.get(key)
        if observations is not None:
            retained = deque(
                item
                for item in observations
                if item.sequence > batch.through_sequence
            )
            if retained:
                self._buffers[key] = retained
            else:
                self._buffers.pop(key, None)
        return True

    def can_process_batch(self, batch: MemoryBatch) -> bool:
        """Reject a queued snapshot invalidated by privacy/config changes."""
        key = (batch.scope_id, batch.user_id)
        if self._generations.get(key, 0) != batch.generation:
            return False
        return self._is_enabled(
            guild_id=batch.guild_id,
            channel_id=batch.request_channel_id,
            user_id=batch.user_id,
        )

    @staticmethod
    def _can_manage_server(interaction: discord.Interaction) -> bool:
        permissions = getattr(interaction.user, "guild_permissions", None)
        return bool(
            permissions
            and (permissions.manage_guild or permissions.administrator)
        )

    def _register_commands(self) -> None:
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
                state = "enabled" if pair in self._enabled_channels else "disabled"
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
                self._enabled_channels.add(pair)
                text = (
                    "🧠 **Persistent bot memory is now enabled in this channel.**\n"
                    "The bot may learn compact facts and preferences from members' "
                    "text messages and use them when that same member mentions it. "
                    "Raw messages are kept only in a small temporary RAM buffer; only "
                    "the summarized profile is saved. Use `/memory-show`, "
                    "`/memory-forget`, or `/memory-opt-out` for personal control."
                )
            else:
                self._enabled_channels.discard(pair)
                self._invalidate_channel(interaction.guild_id, interaction.channel_id)
                text = (
                    "🧠 Persistent bot memory is now **disabled** in this channel. "
                    "Existing compact profiles are retained until forgotten or purged."
                )
            await interaction.response.send_message(text)

        @self.tree.command(
            name="llm-memory-purge",
            description="Permanently delete all saved user profiles for this server",
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
                    "Could not purge saved profiles. Please try again.", ephemeral=True
                )
                return
            self._purge_buffers(interaction.guild_id)
            await interaction.response.send_message(
                f"🧠 Purged **{removed}** saved user profile(s) for this server. "
                "Channel settings and individual opt-outs were preserved."
            )

        @self.tree.command(
            name="memory-show", description="Privately show what the bot remembers about you"
        )
        async def memory_show(interaction: discord.Interaction):
            scope_id = _scope_id(interaction.guild_id)
            profile = db.get_llm_user_memory(scope_id, interaction.user.id)
            preference = self._preference(scope_id, interaction.user.id)
            if preference is False:
                await _send_private(
                    interaction,
                    "You are opted out of persistent memory in this scope, and no "
                    "saved profile is being used.",
                )
            elif not profile:
                await _send_private(
                    interaction, "The bot does not have a saved profile for you here yet."
                )
            else:
                await _send_private(interaction, f"**What I remember about you**\n{profile}")

        @self.tree.command(
            name="memory-forget",
            description="Erase your saved profile here without opting out",
        )
        async def memory_forget(interaction: discord.Interaction):
            scope_id = _scope_id(interaction.guild_id)
            self._invalidate_key((scope_id, interaction.user.id), clear=True)
            removed = db.delete_llm_user_memory(scope_id, interaction.user.id)
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
            self._preference_cache[(scope_id, interaction.user.id)] = False
            self._invalidate_key((scope_id, interaction.user.id), clear=True)
            removed = db.delete_llm_user_memory(scope_id, interaction.user.id)
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
            self._preference_cache[(scope_id, interaction.user.id)] = True
            location = (
                "this DM"
                if interaction.guild_id is None
                else "memory-enabled channels in this server"
            )
            await interaction.response.send_message(
                f"Persistent memory is enabled for you in {location}.", ephemeral=True
            )
