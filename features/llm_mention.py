from __future__ import annotations

import asyncio
import html
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field

import discord
from discord import app_commands

import db
from features.llm_feedback import LLMFeedbackFeature
from features.user_memory import MemoryBatch, UserMemoryFeature
from llm_client import (
    LlamaCppError,
    get_allowed_models,
    get_mention_model,
    llama_supports_vision,
)
from mention_utils import extract_mention_text, resolve_bot_display_name
from tease_llm import (
    MentionResult,
    generate_memory_delta,
    generate_mention_result,
    generate_ordinary_reaction,
    generate_summon_reply,
)

logger = logging.getLogger("discord_bot")


def get_ask_cooldown_seconds() -> float:
    return float(os.getenv("ASK_COOLDOWN_SECONDS", "60"))


def get_llm_context_messages() -> int:
    return int(os.getenv("LLM_CONTEXT_MESSAGES", "0"))


def get_reaction_chance() -> float:
    return min(1.0, max(0.0, float(os.getenv("LLM_REACTION_CHANCE", "0.10"))))


def get_reaction_cooldown_seconds() -> float:
    return max(0.0, float(os.getenv("LLM_REACTION_COOLDOWN_SECONDS", "60")))


def get_memory_consolidation_interval_seconds() -> float:
    return max(
        1.0,
        float(os.getenv("LLM_MEMORY_CONSOLIDATION_INTERVAL_SECONDS", "300")),
    )


def get_model_choices() -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=model, value=model)
        for model in get_allowed_models()
    ]


def get_selected_model() -> str:
    """Return a valid configured alias and repair stale pre-migration values."""
    stored = db.get_setting("mention_model")
    allowed = set(get_allowed_models())
    if stored in allowed:
        return stored

    fallback = get_mention_model()
    if stored:
        logger.warning(
            "Stored mention model %r is not a configured llama.cpp alias; using %r.",
            stored,
            fallback,
        )
        db.set_setting("mention_model", fallback)
    return fallback


DISCORD_MESSAGE_LIMIT = 2000
DISCORD_SAFE_LIMIT = 1990
MENTION_PROMPT_VERSION = "mention-v5-structured"
MEMORY_MENTION_PROMPT_VERSION = "mention-v6-structured-memory"
VISION_MENTION_PROMPT_VERSION = "mention-v7-structured-vision"
VISION_MEMORY_MENTION_PROMPT_VERSION = "mention-v8-structured-vision-memory"
SUMMON_PROMPT_VERSION = "summon-v1"
REFERENCE_CONTEXT_CHAR_BUDGET = 6000
VISION_REFERENCE_CONTEXT_CHAR_BUDGET = 4000
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
IMAGE_DESCRIPTION_REQUEST = (
    "Describe the visible contents of the attached image accurately and concisely."
)
_IMAGE_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
_KNOWN_IMAGE_EXTENSIONS = set(_IMAGE_EXTENSIONS) | {
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".webp",
}


def _normalized_content_type(attachment: discord.Attachment) -> str:
    content_type = (getattr(attachment, "content_type", None) or "").lower()
    return content_type.partition(";")[0].strip()


def _is_image_attachment(attachment: discord.Attachment) -> bool:
    content_type = _normalized_content_type(attachment)
    suffix = os.path.splitext(getattr(attachment, "filename", ""))[1].lower()
    return content_type.startswith("image/") or suffix in _KNOWN_IMAGE_EXTENSIONS


def _expected_image_mime(attachment: discord.Attachment) -> str | None:
    content_type = _normalized_content_type(attachment)
    if content_type in {"image/jpeg", "image/jpg"}:
        return "image/jpeg"
    if content_type == "image/png":
        return "image/png"
    if content_type.startswith("image/"):
        return None
    suffix = os.path.splitext(getattr(attachment, "filename", ""))[1].lower()
    return _IMAGE_EXTENSIONS.get(suffix)


def select_image_attachment(
    attachments: list[discord.Attachment],
) -> tuple[discord.Attachment | None, str | None, int, str | None]:
    """Select and validate the first direct image using Discord metadata."""
    images = [
        attachment for attachment in attachments if _is_image_attachment(attachment)
    ]
    if not images:
        return None, None, 0, None

    attachment = images[0]
    mime = _expected_image_mime(attachment)
    if mime is None:
        return (
            None,
            None,
            len(images),
            "I can currently inspect only PNG or JPEG images.",
        )
    size = getattr(attachment, "size", None)
    if not isinstance(size, int) or size < 1 or size > MAX_IMAGE_BYTES:
        return (
            None,
            None,
            len(images),
            "That image is too large. Please use a PNG or JPEG under 8 MiB.",
        )
    width = getattr(attachment, "width", None)
    height = getattr(attachment, "height", None)
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or width < 1
        or not isinstance(height, int)
        or isinstance(height, bool)
        or height < 1
    ):
        return None, None, len(images), "I could not validate that image's dimensions."
    if width * height > MAX_IMAGE_PIXELS:
        return (
            None,
            None,
            len(images),
            "That image has too many pixels. Please use an image under 25 megapixels.",
        )
    return attachment, mime, len(images), None


def detect_image_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


def budget_reference_context(
    user_memory: str,
    context_messages: list[str],
    *,
    max_chars: int = REFERENCE_CONTEXT_CHAR_BUDGET,
) -> tuple[str, list[str]]:
    """Reserve half for memory and half for history, sharing unused capacity."""
    def escaped_prefix(value: str, budget: int) -> tuple[str, int]:
        used = 0
        end = 0
        for end, character in enumerate(value, start=1):
            cost = len(html.escape(character, quote=False))
            if used + cost > budget:
                return value[:end - 1], used
            used += cost
        return value[:end], used

    def select_history(budget: int) -> tuple[list[str], int]:
        remaining = budget
        selected_reversed = []
        used = 0
        for message in reversed(context_messages):
            if remaining <= 0:
                break
            cost = len(html.escape(message, quote=False))
            if cost <= remaining:
                selected_reversed.append(message)
                remaining -= cost
                used += cost
                continue
            if not selected_reversed:
                partial, partial_cost = escaped_prefix(message, remaining)
                if partial:
                    selected_reversed.append(partial)
                    used += partial_cost
            break
        return list(reversed(selected_reversed)), used

    half = max_chars // 2
    memory, memory_cost = escaped_prefix(user_memory, half)
    history_budget = max_chars - memory_cost if memory_cost < half else half
    history, history_cost = select_history(history_budget)
    if history_cost < half and len(memory) < len(user_memory):
        memory, memory_cost = escaped_prefix(user_memory, max_chars - history_cost)
    elif memory_cost < half and context_messages:
        history, _ = select_history(max_chars - memory_cost)
    return memory, history


def strip_leading_reply_labels(
    text: str, *, requester_id: int, names: tuple[str, ...]
) -> str:
    """Remove model-added addressing only at the beginning of a reply."""
    cleaned = text.strip()
    cleaned = re.sub(rf"^(?:\s*<@!?{requester_id}>\s*)+", "", cleaned)
    usable = sorted(
        {name.strip() for name in names if name and name.strip()},
        key=len,
        reverse=True,
    )
    if not usable:
        return cleaned
    alternatives = "|".join(re.escape(name) for name in usable)
    leading_at_name = re.compile(
        rf"^\s*(?:[*_`~]{{1,3}})?\s*@(?:{alternatives})\b\s*"
        rf"(?:[*_`~]{{1,3}})?\s*",
        flags=re.IGNORECASE,
    )
    while True:
        updated = leading_at_name.sub("", cleaned, count=1).lstrip()
        if updated == cleaned:
            break
        cleaned = updated
    label = re.compile(
        rf"^\s*(?:[*_`~]{{1,3}})?\s*"
        rf"(?:@?(?:{alternatives}))(?:\s+@?(?:{alternatives}))*"
        rf"\s*(?:[*_`~]{{1,3}})?\s*[:：\-–—]\s*"
        rf"(?:[*_`~]{{1,3}})?\s*",
        flags=re.IGNORECASE,
    )
    while True:
        updated = label.sub("", cleaned, count=1).lstrip()
        if updated == cleaned:
            break
        cleaned = updated
    return cleaned or text.strip()


def split_discord_messages(text: str, *, first_prefix: str = "") -> list[str]:
    """Split `text` into messages that fit Discord's 2000-character limit."""
    if not text and not first_prefix:
        return []

    chunks: list[str] = []
    remaining = text
    prefix = first_prefix
    while remaining or prefix:
        cap = DISCORD_SAFE_LIMIT - len(prefix)
        if cap < 1:
            chunks.append(prefix[:DISCORD_SAFE_LIMIT])
            prefix = ""
            continue

        if not remaining:
            chunks.append(prefix)
            break

        if len(remaining) <= cap:
            chunks.append(prefix + remaining)
            break

        split_at = remaining.rfind("\n\n", 0, cap)
        if split_at <= 0:
            split_at = remaining.rfind("\n", 0, cap)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, cap)
        if split_at <= 0:
            split_at = cap

        chunks.append(prefix + remaining[:split_at])
        remaining = remaining[split_at:].lstrip()
        prefix = ""

    return chunks


def requests_ahead(*, processing: bool, queue_size: int) -> int:
    return queue_size + (1 if processing else 0)


@dataclass
class AskJob:
    user: discord.abc.User
    question: str
    model: str
    channel: discord.abc.Messageable | None = None
    reply_to: discord.Message | None = None
    summon_only: bool = False
    context_messages: list[str] = field(default_factory=list)
    user_memory: str = ""
    memory_enabled: bool = False
    memory_batch: MemoryBatch | None = None
    image_attachment: discord.Attachment | None = None
    image_mime: str | None = None
    prompt_version: str = MENTION_PROMPT_VERSION
    replied_message: str = ""
    bot_names: tuple[str, ...] = ()


@dataclass
class MemoryJob:
    batch: MemoryBatch
    model: str


@dataclass
class ReactionJob:
    message: discord.Message
    model: str
    channel_id: int


class ContextReactionFeature:
    """Dispatch adapter placing ordinary reactions after keyword handling."""

    def __init__(self, mention_feature: "LLMMentionFeature"):
        self.mention_feature = mention_feature

    async def handle_message(self, message: discord.Message) -> bool:
        return await self.mention_feature.handle_ordinary_message(message)


class LLMMentionFeature:
    """@bot mention prompts via llama.cpp and /llm-set command."""

    def __init__(
        self,
        client: discord.Client,
        tree: app_commands.CommandTree,
        *,
        bot_id: int,
        feedback: LLMFeedbackFeature | None = None,
        memory: UserMemoryFeature | None = None,
    ):
        self.client = client
        self.tree = tree
        self.bot_id = bot_id
        self.feedback = feedback
        self.memory = memory
        self._user_last_ask: dict[int, float] = {}
        self._user_pending: set[int] = set()
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._queue_sequence = 0
        self._processing = False
        self._worker_task: asyncio.Task | None = None
        self._memory_scheduler_task: asyncio.Task | None = None
        self._memory_pending: set[tuple[int, int]] = set()
        self._reaction_pending_channels: set[int] = set()
        self._reaction_last_attempt: dict[int, float] = {}
        self._register_commands()

    def _cooldown_remaining(self, user_id: int) -> float:
        last = self._user_last_ask.get(user_id, 0.0)
        return max(0.0, get_ask_cooldown_seconds() - (time.time() - last))

    def _ensure_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._queue_worker())

    async def _queue_worker(self) -> None:
        while True:
            queued = await self._queue.get()
            job = queued[2] if isinstance(queued, tuple) else queued
            try:
                self._processing = True
                if isinstance(job, AskJob):
                    await self._process_job(job)
                elif isinstance(job, MemoryJob):
                    await self._process_memory_job(job)
                else:
                    await self._process_reaction_job(job)
            except Exception:
                logger.exception("Unhandled error processing LLM job")
            finally:
                self._processing = False
                self._queue.task_done()
                if isinstance(job, AskJob):
                    self._user_pending.discard(job.user.id)
                    self._user_last_ask[job.user.id] = time.time()
                elif isinstance(job, MemoryJob):
                    self._memory_pending.discard(
                        (job.batch.scope_id, job.batch.user_id)
                    )
                else:
                    self._reaction_pending_channels.discard(job.channel_id)

    async def _put_job(self, priority: int, job) -> None:
        self._queue_sequence += 1
        await self._queue.put((priority, self._queue_sequence, job))
        self._ensure_worker()

    async def start_tasks(self) -> None:
        self._ensure_worker()
        if self._memory_scheduler_task is None or self._memory_scheduler_task.done():
            self._memory_scheduler_task = asyncio.create_task(
                self._memory_scheduler_loop()
            )

    async def _memory_scheduler_loop(self) -> None:
        while True:
            await asyncio.sleep(get_memory_consolidation_interval_seconds())
            if self.memory is None:
                continue
            for batch in self.memory.eligible_batches():
                await self._enqueue_memory(batch, get_selected_model())

    async def _enqueue_memory(self, batch: MemoryBatch, model: str) -> None:
        key = (batch.scope_id, batch.user_id)
        if key in self._memory_pending:
            return
        self._memory_pending.add(key)
        await self._put_job(1, MemoryJob(batch=batch, model=model))

    async def _reply_mention(self, job: AskJob, text: str) -> str:
        if not text.strip() or job.reply_to is None:
            return ""
        user_names = (
            getattr(job.user, "display_name", ""),
            getattr(job.user, "name", ""),
        )
        text = strip_leading_reply_labels(
            text,
            requester_id=job.user.id,
            names=tuple(user_names) + tuple(job.bot_names),
        )
        parts = split_discord_messages(text, first_prefix=f"<@{job.user.id}> ")
        reply = await job.reply_to.reply(
            parts[0],
            mention_author=False,
            allowed_mentions=discord.AllowedMentions(
                users=[job.user], roles=False, everyone=False, replied_user=False
            ),
        )
        if self.feedback is not None:
            await self.feedback.register_reply(
                reply,
                requester_user_id=job.user.id,
                category="summon" if job.summon_only else "mention",
                model=job.model,
                prompt_version=job.prompt_version,
            )
        if job.channel is not None:
            for part in parts[1:]:
                await job.channel.send(
                    part,
                    reference=job.reply_to,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        return text

    @staticmethod
    async def _reply_job_error(job: AskJob, text: str) -> None:
        if job.reply_to is None:
            return
        await job.reply_to.reply(
            text,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _process_job(self, job: AskJob) -> None:
        image_bytes = None
        if job.image_attachment is not None:
            try:
                vision_enabled = await asyncio.to_thread(llama_supports_vision)
            except LlamaCppError as exc:
                logger.warning("Could not verify llama.cpp vision support: %s", exc)
                await self._reply_job_error(
                    job,
                    "I can't inspect images right now because the vision service is unavailable.",
                )
                return
            if not vision_enabled:
                await self._reply_job_error(
                    job,
                    "I can't inspect images because llama.cpp vision support is disabled.",
                )
                return
            try:
                image_bytes = await job.image_attachment.read()
            except Exception as exc:
                logger.warning(
                    "Discord image download failed (%s)", type(exc).__name__
                )
                await self._reply_job_error(
                    job,
                    "I couldn't download that image. It may have been deleted; please upload it again.",
                )
                return
            if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
                await self._reply_job_error(
                    job,
                    "The downloaded image is empty or exceeds the 8 MiB limit.",
                )
                return
            detected_mime = detect_image_mime(image_bytes)
            if detected_mime is None or detected_mime != job.image_mime:
                await self._reply_job_error(
                    job,
                    "That attachment is not a valid PNG or JPEG image.",
                )
                return

        try:
            if job.summon_only:
                reply = await asyncio.to_thread(
                    generate_summon_reply,
                    job.user.display_name,
                    model=job.model,
                )
                result = MentionResult(reply) if reply else None
            else:
                generation_kwargs = {
                    "model": job.model,
                    "context_messages": job.context_messages,
                    "user_memory": job.user_memory,
                    "memory_enabled": job.memory_enabled,
                    "image_bytes": image_bytes,
                    "image_mime": job.image_mime,
                    "replied_message": job.replied_message,
                }
                result = await asyncio.to_thread(
                    generate_mention_result,
                    job.user.display_name,
                    job.question,
                    **generation_kwargs,
                )
        except Exception:
            logger.exception("Unexpected error in mention reply")
            await self._reply_job_error(
                job,
                "I couldn't process that image. Please try again in a moment."
                if job.image_attachment is not None
                else "I couldn't generate a reliable answer. Please try again.",
            )
            return

        if result is None or not result.text:
            await self._reply_job_error(
                job,
                "I couldn't process that image. Please try again in a moment."
                if job.image_attachment is not None
                else "I couldn't generate a reliable answer. Please try again.",
            )
            return

        sent_text = await self._reply_mention(job, result.text)
        if result.reaction and job.reply_to is not None:
            reaction_channel_id = getattr(
                getattr(job.reply_to, "channel", None), "id", None
            )
            now = time.monotonic()
            if (
                reaction_channel_id is not None
                and self._reaction_channel_available(reaction_channel_id, now)
            ):
                self._reaction_last_attempt[reaction_channel_id] = now
                try:
                    await job.reply_to.add_reaction(result.reaction)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
                    logger.info("Could not add mention reaction: %s", exc)

        if (
            sent_text
            and self.memory is not None
            and hasattr(self.memory, "record_bot_reply")
            and job.memory_batch is not None
        ):
            channel_id = getattr(job.channel, "id", job.memory_batch.request_channel_id)
            self.memory.record_bot_reply(job.memory_batch, channel_id, sent_text)

        if (
            not job.summon_only
            and self.memory is not None
            and job.memory_batch is not None
            and self.memory.can_process_batch(job.memory_batch)
        ):
            await self._enqueue_memory(job.memory_batch, job.model)

    async def _process_memory_job(self, job: MemoryJob) -> None:
        if self.memory is None or not self.memory.can_process_batch(job.batch):
            return
        entries = self.memory.entries_for_batch(job.batch)
        result = await asyncio.to_thread(
            generate_memory_delta,
            entries,
            list(job.batch.observations),
            model=job.model,
        )
        if result.successful and self.memory.can_process_batch(job.batch):
            self.memory.commit_delta(job.batch, result.additions, result.corrections)

    async def _process_reaction_job(self, job: ReactionJob) -> None:
        content = getattr(job.message, "clean_content", "").strip()
        if not content:
            return
        reaction = await asyncio.to_thread(
            generate_ordinary_reaction,
            getattr(job.message.author, "display_name", "User"),
            content,
            model=job.model,
        )
        if not reaction:
            return
        try:
            await job.message.add_reaction(reaction)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            logger.info("Could not add ordinary-message reaction: %s", exc)

    def _reaction_channel_available(self, channel_id: int, now: float) -> bool:
        return (
            channel_id not in self._reaction_pending_channels
            and (
                channel_id not in self._reaction_last_attempt
                or now - self._reaction_last_attempt[channel_id]
                >= get_reaction_cooldown_seconds()
            )
        )

    async def handle_ordinary_message(self, message: discord.Message) -> bool:
        """Occasionally queue one reaction while keeping mention work first."""
        content = getattr(message, "clean_content", "").strip()
        channel_id = getattr(message.channel, "id", None)
        if not content or channel_id is None or random.random() >= get_reaction_chance():
            return False
        now = time.monotonic()
        if (
            self._processing
            or self._queue.qsize() > 0
            or not self._reaction_channel_available(channel_id, now)
        ):
            return False
        self._reaction_last_attempt[channel_id] = now
        self._reaction_pending_channels.add(channel_id)
        await self._put_job(
            2,
            ReactionJob(
                message=message,
                model=get_selected_model(),
                channel_id=channel_id,
            ),
        )
        return True

    def _begin_job_checks(self, user_id: int) -> str | None:
        if user_id in self._user_pending:
            return "I'm already working on something for you — hang on."
        remaining = self._cooldown_remaining(user_id)
        if remaining > 0:
            return f"Please wait **{int(remaining) + 1}s** before trying again."
        return None

    async def _enqueue_job(self, job: AskJob) -> None:
        self._user_pending.add(job.user.id)
        await self._put_job(0, job)

    async def handle_message(self, message: discord.Message) -> bool:
        text = extract_mention_text(message, self.bot_id)
        if text is None:
            return False

        image_attachment, image_mime, image_count, image_error = (
            select_image_attachment(list(getattr(message, "attachments", [])))
        )
        if image_error is not None:
            await message.reply(
                image_error,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True

        user_id = message.author.id
        blocked = self._begin_job_checks(user_id)
        if blocked:
            await message.reply(blocked, mention_author=False)
            return True

        has_image = image_attachment is not None
        summon_only = not text and not has_image
        question = text or (IMAGE_DESCRIPTION_REQUEST if has_image else "")
        logger.info(
            "Bot mention from %s (summon=%s, image=%s, len=%s)",
            message.author,
            summon_only,
            has_image,
            len(text),
        )

        model = get_selected_model()

        limit = get_llm_context_messages()
        context_messages = []
        if limit > 0:
            async for past_msg in message.channel.history(
                limit=limit, before=message
            ):
                context_messages.append(
                    f"{past_msg.author.display_name}: {past_msg.clean_content}"
                )
            context_messages.reverse()

        memory_enabled = False
        user_memory = ""
        memory_batch = None
        memory_history = []
        if not summon_only and self.memory is not None:
            memory_context = self.memory.context_for(message)
            memory_enabled = memory_context.enabled
            user_memory = memory_context.profile
            memory_batch = memory_context.batch
            memory_history = list(getattr(memory_context, "history", ()))

        # Persistent per-user conversation precedes live channel context so
        # the newest live messages win when the shared history budget is tight.
        context_messages = memory_history + context_messages

        user_memory, context_messages = budget_reference_context(
            user_memory,
            context_messages,
            max_chars=(
                VISION_REFERENCE_CONTEXT_CHAR_BUDGET
                if has_image
                else REFERENCE_CONTEXT_CHAR_BUDGET
            ),
        )

        replied_message = ""
        reference = getattr(message, "reference", None)
        resolved = getattr(reference, "resolved", None)
        if isinstance(resolved, discord.Message):
            replied_content = resolved.clean_content.strip()
            if replied_content:
                replied_message = (
                    f"{resolved.author.display_name}: {replied_content}"
                )

        bot_names = []
        client = getattr(self, "client", None)
        live_display_name = (
            resolve_bot_display_name(message.guild, client) if client is not None else ""
        )
        if live_display_name:
            bot_names.append(live_display_name)
        client_user = getattr(client, "user", None)
        for value in (
            getattr(client_user, "display_name", ""),
            getattr(client_user, "name", ""),
            getattr(getattr(message.guild, "me", None), "name", ""),
        ):
            if value and value not in bot_names:
                bot_names.append(value)

        job = AskJob(
            user=message.author,
            question=question,
            model=model,
            channel=message.channel,
            reply_to=message,
            summon_only=summon_only,
            context_messages=context_messages,
            user_memory=user_memory,
            memory_enabled=memory_enabled,
            memory_batch=memory_batch,
            image_attachment=image_attachment,
            image_mime=image_mime,
            replied_message=replied_message,
            bot_names=tuple(bot_names),
            prompt_version=(
                SUMMON_PROMPT_VERSION
                if summon_only
                else (
                    VISION_MEMORY_MENTION_PROMPT_VERSION
                    if has_image and memory_enabled
                    else (
                        VISION_MENTION_PROMPT_VERSION
                        if has_image
                        else (
                            MEMORY_MENTION_PROMPT_VERSION
                            if memory_enabled
                            else MENTION_PROMPT_VERSION
                        )
                    )
                )
            ),
        )
        ahead = requests_ahead(
            processing=self._processing,
            queue_size=self._queue.qsize(),
        )
        await self._enqueue_job(job)
        notices = []
        if ahead > 0:
            noun = "request" if ahead == 1 else "requests"
            notices.append(
                f"⏳ I'm working on **{ahead} {noun}** already; yours is queued."
            )
        if image_count > 1:
            notices.append(
                "I can inspect one image per request, so I'll use the first image."
            )
        if notices:
            await message.reply(
                "\n".join(notices),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return True

    def _register_commands(self) -> None:
        @self.tree.command(
            name="llm-set",
            description="Set the model used when the bot is mentioned",
        )
        @app_commands.describe(
            model="llama.cpp model alias to use for mentions",
        )
        @app_commands.choices(model=get_model_choices())
        async def llm_set(
            interaction: discord.Interaction,
            model: str,
        ):
            logger.info(f"Command /llm-set called by {interaction.user} (model={model})")
            try:
                allowed = get_allowed_models()
                if model not in allowed:
                    await interaction.response.send_message(
                        f"Model `{model}` is not allowed.", ephemeral=True
                    )
                    return
                    
                db.set_setting("mention_model", model)
                await interaction.response.send_message(
                    f"Mention model successfully set to **{model}**.", ephemeral=True
                )
            except Exception:
                logger.exception("Failed to set mention model")
                await interaction.response.send_message(
                    "An error occurred while setting the model.", ephemeral=True
                )
