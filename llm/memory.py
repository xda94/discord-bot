"""Persistent memory storage, retrieval, batching, and commit guards."""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from dataclasses import dataclass

import db

logger = logging.getLogger("discord_bot")

logger = logging.getLogger("discord_bot")

DM_SCOPE_ID = 0
OBSERVATION_MESSAGE_MAX_CHARS = 2000
# Keep extraction small enough for the CPU host's short context. These are
# character budgets, not tokenizer-specific context guarantees.
SYNTHESIS_CHUNK_MAX_CHARS = 3000
SYNTHESIS_CHUNK_MAX_MESSAGES = 10
SYNTHESIS_ENTRIES_MAX_CHARS = 2000
PRIVATE_MESSAGE_CHUNK = 1900
MEMORY_CYCLE_MIN_OBSERVATIONS = 50
MEMORY_OBSERVATION_MIN_AGE_SECONDS = 10 * 60
# Import-compatible alias for callers that imported the old constant. The
# scheduler no longer uses a daily age threshold.
MEMORY_SYNTHESIS_INTERVAL_SECONDS = MEMORY_OBSERVATION_MIN_AGE_SECONDS
MEMORY_CONFIG_CACHE_SECONDS = 30
MANUAL_MEMORY_ENTRY_MAX_CHARS = 200
MANUAL_MEMORY_TYPES = {
    "like": "Likes",
    "dislike": "Dislikes",
    "fact": "Facts",
    "interest": "Interests",
    "opinion": "Opinions",
    "other": "Other",
}
MEMORY_KIND_LABELS = {
    **MANUAL_MEMORY_TYPES,
    "impression": "Impressions",
    "topic": "Topics",
}


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
    cycle_endpoint_observation_id: int | None = None
    oldest_observation_created_at: float = 0.0


@dataclass(frozen=True)
class MemoryContext:
    enabled: bool
    profile: str = ""
    history: tuple[str, ...] = ()
    batch: MemoryBatch | None = None
def _scope_id(guild_id: int | None) -> int:
    return guild_id if guild_id is not None else DM_SCOPE_ID


class MemoryStore:
    """Own process-local memory state backed by SQLite."""

    def __init__(self, *, automatic_enabled: bool = True):
        self.automatic_enabled = automatic_enabled
        self._enabled_channels: set[tuple[int, int]] = (
            set(db.get_enabled_llm_memory_channels()) if automatic_enabled else set()
        )
        self._enabled_channels_checked_at = time.monotonic()
        self._preference_cache: dict[tuple[int, int], bool | None] = {}
        self._preference_checked_at: dict[tuple[int, int], float] = {}
        self._buffers: dict[tuple[int, int], deque[MemoryObservation]] = {}
        self._generations: dict[tuple[int, int], int] = {}
        if automatic_enabled:
            self._load_pending_observations()

    def _load_pending_observations(self) -> None:
        """Recover unprocessed user-authored messages after a restart."""
        rows = db.get_llm_memory_observations()
        for row_id, scope_id, user_id, _guild_id, channel_id, content, created_at in rows:
            self._buffers.setdefault((scope_id, user_id), deque()).append(
                MemoryObservation(row_id, channel_id, content, created_at)
            )
        logger.info(
            "Memory observations loaded observations=%d owners=%d",
            len(rows),
            len(self._buffers),
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
        logger.info(
            "Memory buffer reconciled scope_id=%s user_id=%s before=%d after=%d generation=%d",
            scope_id,
            user_id,
            len(current_ids),
            len(saved_ids),
            self._generations[key],
        )

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
            self._buffers.pop(key, None)
            # Include rows written by the API or another process, not only
            # this feature instance's deque.
            observation_ids = (
                row[0]
                for row in db.get_llm_memory_observations(*key)
            )
            db.delete_llm_memory_observations(observation_ids)

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
        content = content[:OBSERVATION_MESSAGE_MAX_CHARS]
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
        logger.debug(
            "Memory observation captured scope_id=%s user_id=%s channel_id=%s "
            "observation_id=%s chars=%d pending=%d",
            scope_id,
            user_id,
            channel_id,
            sequence,
            len(content),
            len(observations),
        )

    def capture_message(
        self,
        *,
        user_id: int,
        guild_id: int | None,
        channel_id: int,
        content: str,
    ) -> bool:
        if not self.automatic_enabled:
            return False
        content = content.strip()
        if not content:
            return False
        if not self._is_enabled(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
        ):
            return False
        self._append_observation(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            content=content,
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

    def context_for(
        self,
        *,
        guild_id: int | None,
        channel_id: int,
        user_id: int,
        query: str,
    ) -> MemoryContext:
        """Snapshot the requester's synthesized profile and pending observations."""
        scope_id = _scope_id(guild_id)
        if not self.automatic_enabled:
            profile = self._selected_entry_text(scope_id, user_id, query)
            if not profile:
                profile = db.get_llm_user_memory(scope_id, user_id) or ""
            return MemoryContext(enabled=bool(profile), profile=profile)

        if not self._is_enabled(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
        ):
            return MemoryContext(enabled=False)

        key = (scope_id, user_id)
        self._sync_buffer(key)
        profile = self._selected_entry_text(scope_id, user_id, query)
        # A legacy profile created after startup remains readable until the
        # next init_db migration (useful to API callers during a rolling update).
        if not profile:
            profile = db.get_llm_user_memory(scope_id, user_id) or ""
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
        return MemoryContext(enabled=True, profile=profile, batch=batch)

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
            batch.scope_id,
            batch.user_id,
            additions,
            corrections,
            ids,
            channel_id=(
                batch.request_channel_id
                if batch.cycle_endpoint_observation_id is not None
                else None
            ),
            cycle_endpoint_observation_id=batch.cycle_endpoint_observation_id,
        ):
            logger.warning(
                "Memory delta database commit failed scope_id=%s user_id=%s "
                "additions=%d corrections=%d observations=%d",
                batch.scope_id,
                batch.user_id,
                len(additions),
                len(corrections),
                len(ids),
            )
            return False
        key = (batch.scope_id, batch.user_id)
        observations = self._buffers.get(key)
        if observations is not None:
            acknowledged_ids = set(ids)
            retained = deque(
                item for item in observations if item.sequence not in acknowledged_ids
            )
            if retained:
                self._buffers[key] = retained
            else:
                self._buffers.pop(key, None)
        logger.info(
            "Memory delta committed scope_id=%s user_id=%s additions=%d "
            "corrections=%d observations_removed=%d observations_remaining=%d",
            batch.scope_id,
            batch.user_id,
            len(additions),
            len(corrections),
            len(ids),
            len(self._buffers.get(key, ())),
        )
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
            if used + len(content) > SYNTHESIS_ENTRIES_MAX_CHARS:
                continue
            selected.append({"id": entry_id, "kind": kind, "content": content})
            used += len(content)
        return selected

    @staticmethod
    def synthesis_chunks(batch: MemoryBatch) -> tuple[MemoryBatch, ...]:
        """Split one channel-cycle author group without dropping observations."""
        if len(batch.observation_ids) != len(batch.observations):
            # Compatibility for manually constructed batches in callers/tests.
            return (batch,)
        chunks = []
        current_ids = []
        current_messages = []
        current_chars = 0

        def append_chunk() -> None:
            if not current_messages:
                return
            chunks.append(
                MemoryBatch(
                    scope_id=batch.scope_id,
                    user_id=batch.user_id,
                    guild_id=batch.guild_id,
                    request_channel_id=batch.request_channel_id,
                    generation=batch.generation,
                    through_sequence=current_ids[-1],
                    observations=tuple(current_messages),
                    observation_ids=tuple(current_ids),
                    cycle_endpoint_observation_id=batch.cycle_endpoint_observation_id,
                    oldest_observation_created_at=batch.oldest_observation_created_at,
                )
            )

        for observation_id, message in zip(
            batch.observation_ids, batch.observations
        ):
            if current_messages and (
                len(current_messages) >= SYNTHESIS_CHUNK_MAX_MESSAGES
                or current_chars + len(message) > SYNTHESIS_CHUNK_MAX_CHARS
            ):
                append_chunk()
                current_ids = []
                current_messages = []
                current_chars = 0
            current_ids.append(observation_id)
            current_messages.append(message)
            current_chars += len(message)
        append_chunk()
        return tuple(chunks)

    def _memory_channels(self) -> set[tuple[int, int]]:
        """Return enabled guild channels plus DM channels with observations."""
        self._refresh_enabled_channels()
        channels = set(self._enabled_channels)
        channels.update(
            (scope_id, channel_id)
            for scope_id, channel_id in db.get_llm_memory_observation_channels()
            if scope_id == DM_SCOPE_ID
        )
        return channels

    def _permitted_rows(self, scope_id: int, channel_id: int, rows) -> list:
        permitted = []
        invalidated_users = set()
        guild_id = None if scope_id == DM_SCOPE_ID else scope_id
        for row in rows:
            (
                _row_id,
                _scope_id,
                user_id,
                row_guild_id,
                _channel_id,
                _content,
                _created,
            ) = row
            effective_guild_id = guild_id if guild_id is not None else row_guild_id
            if self._is_enabled(
                guild_id=effective_guild_id,
                channel_id=channel_id,
                user_id=user_id,
            ):
                permitted.append(row)
            elif (
                user_id not in invalidated_users
                and self._preference(scope_id, user_id) is False
            ):
                # A row may have been written by another process after the
                # opt-out cache was populated. Remove it before it can block
                # completion of an active channel cycle.
                self._invalidate_key((scope_id, user_id), clear=True)
                invalidated_users.add(user_id)
        return permitted

    def _batches_for_rows(
        self,
        *,
        scope_id: int,
        channel_id: int,
        endpoint: int,
        rows,
    ) -> list[MemoryBatch]:
        grouped: dict[int, list[MemoryObservation]] = {}
        guild_id = None if scope_id == DM_SCOPE_ID else scope_id
        for (
            row_id,
            _scope_id,
            user_id,
            _row_guild_id,
            row_channel_id,
            content,
            created_at,
        ) in rows:
            grouped.setdefault(user_id, []).append(
                MemoryObservation(row_id, row_channel_id, content, created_at)
            )

        batches = []
        for user_id, observations in grouped.items():
            observations.sort(key=lambda item: (item.created_at, item.sequence))
            batches.append(
                MemoryBatch(
                    scope_id=scope_id,
                    user_id=user_id,
                    guild_id=guild_id,
                    request_channel_id=channel_id,
                    generation=self._generations.get((scope_id, user_id), 0),
                    through_sequence=observations[-1].sequence,
                    observations=tuple(item.content for item in observations),
                    observation_ids=tuple(item.sequence for item in observations),
                    cycle_endpoint_observation_id=endpoint,
                    oldest_observation_created_at=observations[0].created_at,
                )
            )
        batches.sort(
            key=lambda batch: (
                min(
                    observation.created_at
                    for observation in grouped[batch.user_id]
                ),
                batch.observation_ids[0],
                batch.user_id,
            )
        )
        return batches

    def eligible_batches(
        self,
        *,
        now: float | None = None,
        allow_new_cycles: bool = True,
    ) -> list[MemoryBatch]:
        """Return author groups for the next chunk in each channel cycle.

        A new cycle is admitted only after 50 permitted observations have
        reached the ten-minute age threshold. Its endpoint is persisted before
        a job is queued, so messages arriving during a multi-check cycle wait
        for the next cycle. Active-cycle scans pass ``allow_new_cycles=False``
        so they cannot admit unrelated work before the next scheduled scan.
        """
        if not self.automatic_enabled:
            return []
        now = time.time() if now is None else now
        cutoff = now - MEMORY_OBSERVATION_MIN_AGE_SECONDS
        result = []
        for key in list(self._buffers):
            self._sync_buffer(key)

        for scope_id, channel_id in sorted(self._memory_channels()):
            checkpoint, endpoint = db.get_llm_memory_progress(scope_id, channel_id)
            if endpoint is None:
                if not allow_new_cycles:
                    continue
                eligible = self._permitted_rows(
                    scope_id,
                    channel_id,
                    db.get_llm_memory_channel_observations(
                        scope_id,
                        channel_id,
                        after_id=checkpoint,
                        max_created_at=cutoff,
                    ),
                )
                if len(eligible) < MEMORY_CYCLE_MIN_OBSERVATIONS:
                    if eligible:
                        logger.info(
                            "Memory cycle skipped scope_id=%s channel_id=%s "
                            "eligible=%d required=%d",
                            scope_id,
                            channel_id,
                            len(eligible),
                            MEMORY_CYCLE_MIN_OBSERVATIONS,
                        )
                    continue
                endpoint = max(row[0] for row in eligible)
                if not db.begin_llm_memory_cycle(scope_id, channel_id, endpoint):
                    logger.info(
                        "Memory cycle skipped scope_id=%s channel_id=%s "
                        "reason=cycle-already-active",
                        scope_id,
                        channel_id,
                    )
                    continue
                logger.info(
                    "Memory cycle admitted scope_id=%s channel_id=%s "
                    "eligible=%d endpoint=%s",
                    scope_id,
                    channel_id,
                    len(eligible),
                    endpoint,
                )
                rows = eligible
            else:
                rows = self._permitted_rows(
                    scope_id,
                    channel_id,
                    db.get_llm_memory_channel_observations(
                        scope_id,
                        channel_id,
                        after_id=checkpoint,
                        through_id=endpoint,
                    ),
                )
                if not rows:
                    if db.complete_llm_memory_cycle_if_empty(scope_id, channel_id):
                        logger.info(
                            "Memory cycle completed scope_id=%s channel_id=%s "
                            "checkpoint=%s",
                            scope_id,
                            channel_id,
                            endpoint,
                        )
                    continue

            result.extend(
                self._batches_for_rows(
                    scope_id=scope_id,
                    channel_id=channel_id,
                    endpoint=endpoint,
                    rows=rows,
                )
            )

        result.sort(
            key=lambda batch: (
                batch.oldest_observation_created_at,
                batch.observation_ids[0],
                batch.scope_id,
                batch.request_channel_id,
                batch.user_id,
            )
        )
        return result

    def can_process_batch(self, batch: MemoryBatch) -> bool:
        """Reject a queued snapshot invalidated by privacy/config changes."""
        if not self.automatic_enabled:
            return False
        key = (batch.scope_id, batch.user_id)
        if self._generations.get(key, 0) != batch.generation:
            logger.info(
                "Memory batch rejected reason=generation-changed scope_id=%s "
                "user_id=%s expected=%s current=%s",
                batch.scope_id,
                batch.user_id,
                batch.generation,
                self._generations.get(key, 0),
            )
            return False
        if batch.observation_ids:
            saved_ids = {
                row[0]
                for row in db.get_llm_memory_observations(batch.scope_id, batch.user_id)
            }
            if not set(batch.observation_ids).issubset(saved_ids):
                logger.info(
                    "Memory batch rejected reason=observations-changed scope_id=%s "
                    "user_id=%s batch_observations=%d saved_observations=%d",
                    batch.scope_id,
                    batch.user_id,
                    len(batch.observation_ids),
                    len(saved_ids),
                )
                self._invalidate_key(key, clear=False)
                self._sync_buffer(key)
                return False
        if batch.cycle_endpoint_observation_id is not None:
            _checkpoint, endpoint = db.get_llm_memory_progress(
                batch.scope_id, batch.request_channel_id
            )
            if endpoint != batch.cycle_endpoint_observation_id:
                logger.info(
                    "Memory batch rejected reason=cycle-changed scope_id=%s "
                    "channel_id=%s expected_endpoint=%s current_endpoint=%s",
                    batch.scope_id,
                    batch.request_channel_id,
                    batch.cycle_endpoint_observation_id,
                    endpoint,
                )
                return False
        return self._is_enabled(
            guild_id=batch.guild_id,
            channel_id=batch.request_channel_id,
            user_id=batch.user_id,
        )
