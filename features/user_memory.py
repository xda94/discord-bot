from __future__ import annotations

import logging
import re
import time
from collections import deque
from dataclasses import dataclass

import discord
from discord import app_commands

import db

logger = logging.getLogger("discord_bot")

DM_SCOPE_ID = 0
OBSERVATION_MAX_CHARS = 2000
OBSERVATION_MAX_MESSAGES = 20
PRIVATE_MESSAGE_CHUNK = 1900
MEMORY_BATCH_MESSAGES = 10
MEMORY_BATCH_MAX_AGE_SECONDS = 5 * 60
MEMORY_CONFIG_CACHE_SECONDS = 30


@dataclass(frozen=True)
class MemoryObservation:
    sequence: int
    channel_id: int
    content: str
    created_at: float = 0.0


@dataclass(frozen=True)
class MemoryBatch:
    scope_id: int
    user_id: int
    guild_id: int | None
    request_channel_id: int
    generation: int
    through_sequence: int
    observations: tuple[str, ...]
    observation_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class MemoryContext:
    enabled: bool
    profile: str = ""
    history: tuple[str, ...] = ()
    batch: MemoryBatch | None = None


def _scope_id(guild_id: int | None) -> int:
    return guild_id if guild_id is not None else DM_SCOPE_ID


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

    SQLite owns stable entries, bounded transcripts, pending observations,
    channel configuration, and explicit user preferences.
    """

    def __init__(self, client: discord.Client, tree: app_commands.CommandTree):
        self.client = client
        self.tree = tree
        self._enabled_channels: set[tuple[int, int]] = set(
            db.get_enabled_llm_memory_channels()
        )
        self._enabled_channels_checked_at = time.monotonic()
        self._preference_cache: dict[tuple[int, int], bool | None] = {}
        self._preference_checked_at: dict[tuple[int, int], float] = {}
        self._buffers: dict[tuple[int, int], deque[MemoryObservation]] = {}
        self._generations: dict[tuple[int, int], int] = {}
        self._load_pending_observations()
        self._register_commands()

    def _load_pending_observations(self) -> None:
        """Recover unprocessed user-authored messages after a restart."""
        for row_id, scope_id, user_id, _guild_id, channel_id, content, created_at in (
            db.get_llm_memory_observations()
        ):
            self._buffers.setdefault((scope_id, user_id), deque()).append(
                MemoryObservation(row_id, channel_id, content, created_at)
            )

    def _sync_buffer(self, key: tuple[int, int]) -> None:
        """Reconcile one process-local queue with API-side deletions."""
        scope_id, user_id = key
        saved = deque(
            MemoryObservation(row_id, channel_id, content, created_at)
            for row_id, _scope_id, _user_id, _guild_id, channel_id, content, created_at
            in db.get_llm_memory_observations(scope_id, user_id)
        )
        current_ids = tuple(item.sequence for item in self._buffers.get(key, ()))
        saved_ids = tuple(item.sequence for item in saved)
        if current_ids == saved_ids:
            return
        self._generations[key] = self._generations.get(key, 0) + 1
        if saved:
            self._buffers[key] = saved
        else:
            self._buffers.pop(key, None)

    @staticmethod
    def _key(guild_id: int | None, user_id: int) -> tuple[int, int]:
        return (_scope_id(guild_id), user_id)

    def _preference(self, scope_id: int, user_id: int) -> bool | None:
        key = (scope_id, user_id)
        now = time.monotonic()
        checked_at = self._preference_checked_at.get(key, 0)
        if key not in self._preference_cache or now - checked_at >= MEMORY_CONFIG_CACHE_SECONDS:
            previous = self._preference_cache.get(key)
            current = db.get_llm_memory_preference(scope_id, user_id)
            self._preference_cache[key] = current
            self._preference_checked_at[key] = now
            if current is False and previous is not False:
                self._invalidate_key(key, clear=True)
        return self._preference_cache[key]

    def _refresh_enabled_channels(self) -> None:
        now = time.monotonic()
        if now - self._enabled_channels_checked_at < MEMORY_CONFIG_CACHE_SECONDS:
            return
        current = set(db.get_enabled_llm_memory_channels())
        removed = self._enabled_channels - current
        self._enabled_channels = current
        self._enabled_channels_checked_at = now
        for guild_id, channel_id in removed:
            self._invalidate_channel(guild_id, channel_id)

    def _is_enabled(
        self,
        *,
        guild_id: int | None,
        channel_id: int,
        user_id: int,
    ) -> bool:
        self._refresh_enabled_channels()
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
            observations = self._buffers.pop(key, None)
            if observations:
                db.delete_llm_memory_observations(item.sequence for item in observations)

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
        db.delete_llm_memory_channel_observations(guild_id, channel_id)

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
        scope_id = _scope_id(guild_id)
        key = (scope_id, user_id)
        observations = self._buffers.setdefault(key, deque())
        content = content[:OBSERVATION_MAX_CHARS]
        created_at = time.time()
        sequence = db.add_llm_memory_observation(
            scope_id,
            user_id,
            guild_id,
            channel_id,
            content,
            created_at=created_at,
        )
        if sequence is None:
            return
        observations.append(
            MemoryObservation(
                sequence=sequence,
                channel_id=channel_id,
                content=content,
                created_at=created_at,
            )
        )

        removed_ids = []
        while len(observations) > OBSERVATION_MAX_MESSAGES:
            removed_ids.append(observations.popleft().sequence)
        while (
            len(observations) > 1
            and sum(len(item.content) for item in observations)
            > OBSERVATION_MAX_CHARS
        ):
            removed_ids.append(observations.popleft().sequence)
        if removed_ids:
            db.delete_llm_memory_observations(removed_ids)

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
        db.add_llm_memory_transcript(
            _scope_id(guild_id),
            message.author.id,
            message.channel.id,
            "user",
            content,
        )
        return False

    @staticmethod
    def _search_words(value: str) -> set[str]:
        return {
            word
            for word in re.findall(r"\w+", value.casefold(), flags=re.UNICODE)
            if len(word) > 2
        }

    def _selected_entry_text(self, scope_id: int, user_id: int, query: str) -> str:
        query_words = self._search_words(query)
        rows = db.get_llm_memory_entries(scope_id, user_id)
        ranked = []
        for row in rows:
            entry_id, kind, content, _source, _created, updated = row
            overlap = len(query_words & self._search_words(content))
            ranked.append((overlap, updated, entry_id, kind, content))
        ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        # Retrieval, rather than storage eviction, keeps prompt input bounded.
        selected = ranked[:50]
        return "\n".join(
            f"- [{kind} #{entry_id}] {content}"
            for _overlap, _updated, entry_id, kind, content in selected
        )

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
        self._sync_buffer(key)
        profile = self._selected_entry_text(
            scope_id, user_id, getattr(message, "clean_content", "")
        )
        # A legacy profile created after startup remains readable until the
        # next init_db migration (useful to API callers during a rolling update).
        if not profile:
            profile = db.get_llm_user_memory(scope_id, user_id) or ""
        history = tuple(
            f"{'User' if role == 'user' else 'Bot'}: {content}"
            for _row_id, _channel_id, role, content, _created_at
            in db.get_llm_memory_transcript(scope_id, user_id)
        )
        current_content = getattr(message, "clean_content", "").strip()
        if history and history[-1] == f"User: {current_content}":
            history = history[:-1]
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
                observation_ids=tuple(item.sequence for item in snapshot),
            )
        return MemoryContext(enabled=True, profile=profile, history=history, batch=batch)

    def commit_delta(self, batch: MemoryBatch, additions, corrections) -> bool:
        """Atomically apply row changes and acknowledge their source observations."""
        if not self.can_process_batch(batch):
            return False
        source_messages = set(batch.observations)
        if any(
            change.get("source_text") not in source_messages
            for change in (*additions, *corrections)
        ):
            logger.warning("Rejected memory delta with an unsupported source message")
            return False
        ids = batch.observation_ids or tuple(
            item.sequence
            for item in self._buffers.get((batch.scope_id, batch.user_id), ())
            if item.sequence <= batch.through_sequence
        )
        if not db.apply_llm_memory_delta(
            batch.scope_id, batch.user_id, additions, corrections, ids
        ):
            return False
        key = (batch.scope_id, batch.user_id)
        observations = self._buffers.get(key)
        if observations is not None:
            retained = deque(
                item for item in observations if item.sequence not in set(ids)
            )
            if retained:
                self._buffers[key] = retained
            else:
                self._buffers.pop(key, None)
        return True

    def entries_for_batch(self, batch: MemoryBatch) -> list[dict]:
        query_words = self._search_words(" ".join(batch.observations))
        ranked = []
        for row in db.get_llm_memory_entries(batch.scope_id, batch.user_id):
            entry_id, kind, content, _source, _created, updated = row
            overlap = len(query_words & self._search_words(content))
            ranked.append((overlap, updated, entry_id, kind, content))
        ranked.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        selected = []
        used = 0
        for _overlap, _updated, entry_id, kind, content in ranked:
            if selected and used + len(content) > 8000:
                break
            selected.append({"id": entry_id, "kind": kind, "content": content})
            used += len(content)
        return selected

    def eligible_batches(self, *, now: float | None = None) -> list[MemoryBatch]:
        now = time.time() if now is None else now
        result = []
        for key in list(self._buffers):
            self._sync_buffer(key)
        for (scope_id, user_id), observations in list(self._buffers.items()):
            if not observations:
                continue
            oldest = observations[0]
            if (
                len(observations) < MEMORY_BATCH_MESSAGES
                and now - oldest.created_at < MEMORY_BATCH_MAX_AGE_SECONDS
            ):
                continue
            latest = observations[-1]
            guild_id = None if scope_id == DM_SCOPE_ID else scope_id
            if not self._is_enabled(
                guild_id=guild_id,
                channel_id=latest.channel_id,
                user_id=user_id,
            ):
                continue
            snapshot = tuple(observations)
            result.append(
                MemoryBatch(
                    scope_id=scope_id,
                    user_id=user_id,
                    guild_id=guild_id,
                    request_channel_id=latest.channel_id,
                    generation=self._generations.get((scope_id, user_id), 0),
                    through_sequence=latest.sequence,
                    observations=tuple(item.content for item in snapshot),
                    observation_ids=tuple(item.sequence for item in snapshot),
                )
            )
        return result

    def record_bot_reply(self, batch: MemoryBatch | None, channel_id: int, text: str) -> None:
        if batch is None or not self.can_process_batch(batch):
            return
        db.add_llm_memory_transcript(
            batch.scope_id, batch.user_id, channel_id, "assistant", text
        )

    def can_process_batch(self, batch: MemoryBatch) -> bool:
        """Reject a queued snapshot invalidated by privacy/config changes."""
        key = (batch.scope_id, batch.user_id)
        if self._generations.get(key, 0) != batch.generation:
            return False
        if batch.observation_ids:
            saved_ids = {
                row[0]
                for row in db.get_llm_memory_observations(batch.scope_id, batch.user_id)
            }
            if not set(batch.observation_ids).issubset(saved_ids):
                self._invalidate_key(key, clear=False)
                self._sync_buffer(key)
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
                self._refresh_enabled_channels()
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
                    "The bot may save durable facts, short topic notes, and a bounded "
                    "seven-day conversation window for each member, then use them only "
                    "when that same member mentions it. Use `/memory-show`, "
                    "`/memory-forget`, or `/memory-opt-out` for personal control."
                )
            else:
                self._enabled_channels.discard(pair)
                self._invalidate_channel(interaction.guild_id, interaction.channel_id)
                text = (
                    "🧠 Persistent bot memory is now **disabled** in this channel. "
                    "Existing saved memory is retained until forgotten or purged."
                )
            self._enabled_channels_checked_at = time.monotonic()
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
            self._purge_buffers(interaction.guild_id)
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
            transcript = db.get_llm_memory_transcript(scope_id, interaction.user.id)
            profile = db.get_llm_user_memory(scope_id, interaction.user.id)
            preference = self._preference(scope_id, interaction.user.id)
            if preference is False:
                await _send_private(
                    interaction,
                    "You are opted out of persistent memory in this scope, and no "
                    "saved profile is being used.",
                )
            elif not entries and not transcript and not profile:
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
                if transcript:
                    sections.append(
                        "**Recent conversation window**\n"
                        + "\n".join(
                            f"{'You' if role == 'user' else 'Bot'}: {content}"
                            for _row_id, _channel_id, role, content, _created in transcript
                        )
                    )
                await _send_private(interaction, "\n".join(sections))

        @self.tree.command(
            name="memory-forget",
            description="Erase your saved profile here without opting out",
        )
        async def memory_forget(interaction: discord.Interaction):
            scope_id = _scope_id(interaction.guild_id)
            self._invalidate_key((scope_id, interaction.user.id), clear=True)
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
            self._preference_cache[(scope_id, interaction.user.id)] = False
            self._preference_checked_at[(scope_id, interaction.user.id)] = time.monotonic()
            self._invalidate_key((scope_id, interaction.user.id), clear=True)
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
            self._preference_cache[(scope_id, interaction.user.id)] = True
            self._preference_checked_at[(scope_id, interaction.user.id)] = time.monotonic()
            location = (
                "this DM"
                if interaction.guild_id is None
                else "memory-enabled channels in this server"
            )
            await interaction.response.send_message(
                f"Persistent memory is enabled for you in {location}.", ephemeral=True
            )
