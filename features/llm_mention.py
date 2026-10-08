from __future__ import annotations

import asyncio
import html
import io
import logging
import os
import random
import time
import warnings
from dataclasses import dataclass, field

import discord
from discord import app_commands
from PIL import Image

import db
from i18n import t, language_for, localize
from assistant_profiles import AssistantProfile, DEFAULT_PROFILE, effective_profile
from analytics import record, record_for
from features.llm_feedback import LLMFeedbackFeature
from features.user_memory import UserMemoryFeature
from llm.client import (
    LlamaCppError,
    get_allowed_models,
    get_mention_model,
    llama_supports_vision,
)
from llm.memory import MemoryBatch, MemoryStore
from llm.capacity import CapacityError, Reservation, busy, reserve
from mention_utils import (
    extract_mention_text,
    resolve_bot_display_name,
    strip_leading_reply_labels,
)
from llm.memory_extraction import generate_memory_delta
from llm.responses import (
    MentionResult,
    format_chat_line,
    generate_mention_result,
    generate_ordinary_reaction,
    generate_summon_reply,
)
from llm.worker import SingleSlotWorker, WorkerResult

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


def get_memory_active_chunk_rest_seconds() -> float:
    return max(
        1.0,
        float(os.getenv("LLM_MEMORY_ACTIVE_CHUNK_REST_SECONDS", "300")),
    )


def get_memory_failure_backoff_seconds(consecutive_failures: int) -> float:
    """Return the post-completion delay for consecutive memory failures."""
    active_rest = get_memory_active_chunk_rest_seconds()
    if consecutive_failures <= 0:
        return active_rest
    cap = max(active_rest, get_memory_consolidation_interval_seconds())
    delay = active_rest
    for _ in range(consecutive_failures):
        delay = min(cap, delay * 2)
        if delay >= cap:
            break
    return delay


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
MENTION_PROMPT_VERSION = "mention-v12-reminders"
MEMORY_MENTION_PROMPT_VERSION = "mention-v12-reminders-memory"
VISION_MENTION_PROMPT_VERSION = "mention-v12-reminders-vision"
VISION_MEMORY_MENTION_PROMPT_VERSION = "mention-v12-reminders-vision-memory"
SUMMON_PROMPT_VERSION = "summon-v3"
REFERENCE_CONTEXT_CHAR_BUDGET = 6000
VISION_REFERENCE_CONTEXT_CHAR_BUDGET = 4000
MAX_MENTIONED_MEMORIES = 2
MENTIONED_MEMORY_CHAR_BUDGET = 1000
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
IMAGE_DESCRIPTION_REQUEST = (
    "Describe the visible contents of the attached image accurately and concisely."
)
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
_KNOWN_IMAGE_EXTENSIONS = set(_IMAGE_EXTENSIONS) | {
    ".heic",
    ".heif",
}


def _normalized_content_type(attachment: discord.Attachment) -> str:
    content_type = (getattr(attachment, "content_type", None) or "").lower()
    return content_type.partition(";")[0].strip()


def _is_image_attachment(attachment: discord.Attachment) -> bool:
    content_type = _normalized_content_type(attachment)
    suffix = os.path.splitext(getattr(attachment, "filename", ""))[1].lower()
    return content_type.startswith("image/") or suffix in _KNOWN_IMAGE_EXTENSIONS


def select_image_attachments(
    attachments: list[discord.Attachment],
) -> tuple[tuple[discord.Attachment, ...], int, str | None]:
    images = [
        attachment for attachment in attachments if _is_image_attachment(attachment)
    ]
    if not images:
        return (), 0, None

    candidates = []
    errors = set()
    for attachment in images:
        size = getattr(attachment, "size", None)
        if isinstance(size, int) and not isinstance(size, bool) and size > MAX_IMAGE_BYTES:
            errors.add("bytes")
            continue
        width = getattr(attachment, "width", None)
        height = getattr(attachment, "height", None)
        if (
            isinstance(width, int)
            and not isinstance(width, bool)
            and width > 0
            and isinstance(height, int)
            and not isinstance(height, bool)
            and height > 0
            and width * height > MAX_IMAGE_PIXELS
        ):
            errors.add("pixels")
            continue
        candidates.append(attachment)
    if candidates:
        return tuple(candidates), len(images), None
    if errors == {"bytes"}:
        error = "The attached images are too large. Please use an image under 8 MiB."
    elif errors == {"pixels"}:
        error = (
            "The attached images have too many pixels. "
            "Please use an image under 25 megapixels."
        )
    else:
        error = (
            "None of the attached images fits the limits. "
            "Please use an image under 8 MiB and 25 megapixels."
        )
    return (), len(images), error


def prepare_image(data: bytes) -> tuple[bytes, str]:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("That image is empty or exceeds the 8 MiB limit.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                image_format = image.format
                if image_format not in {"JPEG", "PNG", "GIF", "WEBP", "BMP", "TIFF"}:
                    raise ValueError(
                        "I can inspect JPEG, PNG, GIF, WebP, BMP, or TIFF images."
                    )
                width, height = image.size
                if width < 1 or height < 1:
                    raise ValueError("I could not validate that image's dimensions.")
                if width * height > MAX_IMAGE_PIXELS:
                    raise ValueError(
                        "That image has too many pixels. Please use an image under 25 megapixels."
                    )
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.seek(0)
                image.load()
                if image_format == "JPEG":
                    return data, "image/jpeg"
                if image_format == "PNG" and not image.is_animated:
                    return data, "image/png"
                mode = (
                    "RGBA"
                    if "A" in image.getbands() or "transparency" in image.info
                    else "RGB"
                )
                output = io.BytesIO()
                image.convert(mode).save(output, format="PNG")
                normalized = output.getvalue()
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise ValueError(
            "That image has too many pixels. Please use an image under 25 megapixels."
        ) from exc
    except (OSError, SyntaxError, EOFError) as exc:
        raise ValueError(
            "I could not decode that image. Please attach a valid JPEG, PNG, GIF, WebP, BMP, or TIFF image."
        ) from exc
    if len(normalized) > MAX_IMAGE_BYTES:
        raise ValueError("That image exceeds the 8 MiB limit after conversion.")
    return normalized, "image/png"


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


def trim_profile(profile: str, max_chars: int) -> str:
    """Keep whole entries, most relevant first, within an escaped-size budget."""
    kept = []
    used = 0
    for line in profile.splitlines():
        cost = len(html.escape(line, quote=False)) + 1
        if used + cost > max_chars:
            break
        kept.append(line)
        used += cost
    return "\n".join(kept)


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
    image_attachments: tuple[discord.Attachment, ...] = ()
    prompt_version: str = MENTION_PROMPT_VERSION
    replied_message: str = ""
    bot_names: tuple[str, ...] = ()
    assistant_profile: AssistantProfile = DEFAULT_PROFILE
    mentioned_memories: tuple[tuple[str, str], ...] = ()
    reservation: Reservation | None = None


@dataclass
class MemoryJob:
    batch: MemoryBatch
    model: str
    reservation: Reservation | None = None


@dataclass
class ReactionJob:
    message: discord.Message
    model: str
    channel_id: int
    reservation: Reservation | None = None


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
        memory: UserMemoryFeature | MemoryStore | None = None,
    ):
        self.client = client
        self.tree = tree
        self.bot_id = bot_id
        self.feedback = feedback
        self.memory = getattr(memory, "store", memory)
        self._user_last_ask: dict[int, float] = {}
        self._user_pending: set[int] = set()
        self._worker = SingleSlotWorker(
            self._process_queued_job,
            self._finish_queued_job,
        )
        # Compatibility aliases for diagnostics and existing tests.
        self._queue = self._worker.queue
        self._queue_sequence = 0
        self._processing = False
        self._worker_task: asyncio.Task | None = None
        self._memory_scheduler_task: asyncio.Task | None = None
        self._memory_pending: set[tuple[int, int]] = set()
        self._memory_last_finished: float | None = None
        self._memory_consecutive_failures = 0
        self._reaction_pending_channels: set[int] = set()
        self._reaction_last_attempt: dict[int, float] = {}
        self._register_commands()

    def _cooldown_remaining(self, user_id: int) -> float:
        last = self._user_last_ask.get(user_id, 0.0)
        return max(0.0, get_ask_cooldown_seconds() - (time.time() - last))

    def _ensure_worker(self) -> None:
        self._worker.start()
        self._worker_task = self._worker.task

    def _model_busy(self) -> bool:
        try:
            return busy()
        except CapacityError:
            return True

    async def _queue_worker(self) -> None:
        await self._worker.run()

    async def _process_queued_job(self, job) -> WorkerResult:
        self._processing = True
        if isinstance(job, AskJob):
            await self._process_job(job)
            return WorkerResult()
        if isinstance(job, MemoryJob):
            succeeded = await self._process_memory_job(job)
            outcome = "failed" if succeeded is False else (
                "skipped" if succeeded is None else "completed"
            )
            return WorkerResult(outcome, succeeded)
        await self._process_reaction_job(job)
        return WorkerResult()

    def _finish_queued_job(self, job, result: WorkerResult) -> None:
        self._processing = False
        if isinstance(job, AskJob):
            self._user_pending.discard(job.user.id)
            self._user_last_ask[job.user.id] = time.time()
            if result.outcome == "skipped":
                asyncio.create_task(self._reply_job_error(
                    job, "The model is busy right now. Please try again shortly."
                ))
        elif isinstance(job, MemoryJob):
            if result.outcome in {"completed", "failed"}:
                self._memory_last_finished = time.monotonic()
                if result.outcome == "failed":
                    self._memory_consecutive_failures = (
                        getattr(self, "_memory_consecutive_failures", 0) + 1
                    )
                else:
                    self._memory_consecutive_failures = 0
            self._memory_pending.discard((job.batch.scope_id, job.batch.user_id))
        else:
            self._reaction_pending_channels.discard(job.channel_id)

    async def _put_job(self, priority: int, job) -> bool:
        try:
            if job.reservation is None:
                job.reservation = reserve(
                    policy="interactive" if isinstance(job, AskJob) else "background"
                )
        except CapacityError:
            return False
        try:
            worker = getattr(self, "_worker", None)
            if worker is not None:
                admitted = await worker.submit(priority, job)
                self._queue_sequence = worker.sequence
                self._processing = worker.processing
                self._worker_task = worker.task
            else:
                self._queue_sequence += 1
                self._queue.put_nowait((job.reservation.ticket, self._queue_sequence, job))
                self._ensure_worker()
                admitted = True
            if not admitted:
                job.reservation.release()
            return admitted
        except BaseException:
            job.reservation.release()
            raise

    async def start_tasks(self) -> None:
        self._ensure_worker()
        if self.memory is None or not getattr(
            self.memory, "automatic_enabled", True
        ):
            logger.info(
                "Memory scheduler disabled mode=%s",
                "disabled" if self.memory is None else "manual",
            )
            return
        if self._memory_scheduler_task is None or self._memory_scheduler_task.done():
            self._memory_scheduler_task = asyncio.create_task(
                self._memory_scheduler_loop()
            )
            logger.info(
                "Memory scheduler started cycle_interval=%.1fs active_chunk_rest=%.1fs",
                get_memory_consolidation_interval_seconds(),
                get_memory_active_chunk_rest_seconds(),
            )

    async def _memory_scheduler_loop(self) -> None:
        cycle_interval = get_memory_consolidation_interval_seconds()
        wait_seconds = get_memory_active_chunk_rest_seconds()
        next_cycle_scan_at = time.monotonic() + cycle_interval
        while True:
            await asyncio.sleep(wait_seconds)
            now = time.monotonic()
            cycle_interval = get_memory_consolidation_interval_seconds()
            active_chunk_rest = get_memory_active_chunk_rest_seconds()
            consecutive_failures = getattr(
                self, "_memory_consecutive_failures", 0
            )
            required_rest = get_memory_failure_backoff_seconds(
                consecutive_failures
            )
            allow_new_cycles = now >= next_cycle_scan_at
            if (
                self._memory_last_finished is not None
                and now - self._memory_last_finished < required_rest
            ):
                logger.info(
                    "Memory scan skipped reason=%s failures=%d remaining=%.1fs",
                    "failure-backoff" if consecutive_failures else "rest-interval",
                    consecutive_failures,
                    required_rest - (now - self._memory_last_finished),
                )
                wait_seconds = max(
                    0.0,
                    self._memory_last_finished + required_rest - now,
                )
                continue
            if self.memory is None:
                logger.info("Memory scan skipped reason=feature-unavailable")
                wait_seconds = cycle_interval
                if allow_new_cycles:
                    next_cycle_scan_at = now + cycle_interval
                continue
            if self._model_busy():
                logger.info(
                    "Memory scan skipped reason=model-busy processing=%s queue_size=%d",
                    getattr(self, "_processing", False),
                    getattr(getattr(self, "_queue", None), "qsize", lambda: 0)(),
                )
                # Retry soon without consuming a scheduled new-cycle scan.
                wait_seconds = active_chunk_rest
                continue
            try:
                reservation = reserve(policy="background")
            except CapacityError:
                wait_seconds = active_chunk_rest
                continue
            try:
                batches = self.memory.eligible_batches(
                    allow_new_cycles=allow_new_cycles
                )
                if allow_new_cycles:
                    next_cycle_scan_at = now + cycle_interval
                logger.info(
                    "Memory scan completed mode=%s eligible_batches=%d",
                    "new-cycle" if allow_new_cycles else "active-cycle",
                    len(batches),
                )
                for batch in batches:
                    admitted = await self._enqueue_memory(
                        batch, get_selected_model(), reservation=reservation
                    )
                    if admitted:
                        reservation = None
                        break
            finally:
                if reservation is not None:
                    reservation.release()
            wait_seconds = (
                active_chunk_rest
                if batches
                else max(0.0, next_cycle_scan_at - now)
            )

    async def _enqueue_memory(
        self, batch: MemoryBatch, model: str, *, reservation: Reservation | None = None
    ) -> bool:
        key = (batch.scope_id, batch.user_id)
        if key in self._memory_pending:
            logger.info(
                "Memory job skipped reason=already-pending scope_id=%s user_id=%s",
                batch.scope_id,
                batch.user_id,
            )
            return False
        self._memory_pending.add(key)
        try:
            admitted = await self._put_job(
                1, MemoryJob(batch=batch, model=model, reservation=reservation)
            )
        except BaseException:
            self._memory_pending.discard(key)
            raise
        if not admitted:
            self._memory_pending.discard(key)
            logger.info(
                "Memory job not admitted scope_id=%s user_id=%s",
                batch.scope_id,
                batch.user_id,
            )
        return admitted

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
        await record_for("automatic", "llm-reply", job.reply_to)
        return text

    @staticmethod
    async def _reply_job_error(job: AskJob, text: str) -> None:
        if job.reply_to is None:
            return
        await record_for("failure", "llm-reply", job.reply_to)
        from action_proposals import action_candidate
        if action_candidate(job.question):
            lang = language_for(job.question,job.assistant_profile)
            await record_for('control',f'natural/unknown/{lang}/llm/model_unavailable',job.reply_to)
        await job.reply_to.reply(
            t("unavailable",language_for(job.question,job.assistant_profile)) if "reliable answer" in text else localize(text,language_for(job.question,job.assistant_profile)),
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _process_job(self, job: AskJob) -> None:
        image_bytes = None
        image_mime = None
        if job.image_attachments:
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
            image_errors = []
            for attachment in job.image_attachments:
                try:
                    downloaded = await attachment.read()
                except Exception as exc:
                    logger.warning(
                        "Discord image download failed (%s)", type(exc).__name__
                    )
                    image_errors.append(
                        "I couldn't download that image. It may have been deleted; please upload it again."
                    )
                    continue
                try:
                    image_bytes, image_mime = await asyncio.to_thread(
                        prepare_image, downloaded
                    )
                except ValueError as exc:
                    image_errors.append(str(exc))
                    continue
                break
            if image_bytes is None:
                error = (
                    image_errors[0]
                    if len(image_errors) == 1
                    else (
                        "I couldn't inspect any of the attached images. Please upload a valid "
                        "JPEG, PNG, GIF, WebP, BMP, or TIFF image under 8 MiB and 25 megapixels. "
                        "If an image was deleted or could not be downloaded, please upload it again."
                    )
                )
                await self._reply_job_error(job, error)
                return

        bot_name = job.bot_names[0] if job.bot_names else ""
        try:
            if job.summon_only:
                reply = await asyncio.to_thread(
                    generate_summon_reply,
                    job.user.display_name,
                    model=job.model,
                    assistant_profile=job.assistant_profile,
                    bot_name=bot_name,
                    recent_messages=job.context_messages,
                )
                result = MentionResult(reply) if reply else None
            else:
                generation_kwargs = {
                    "model": job.model,
                    "context_messages": job.context_messages,
                    "user_memory": job.user_memory,
                    "memory_enabled": job.memory_enabled,
                    "image_bytes": image_bytes,
                    "image_mime": image_mime,
                    "replied_message": job.replied_message,
                    "requester_id": job.user.id,
                    "reply_names": (getattr(job.user, "name", ""), *job.bot_names),
                    "assistant_profile": job.assistant_profile,
                    "bot_name": bot_name,
                    "mentioned_memories": job.mentioned_memories,
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
                if job.image_attachments
                else "I couldn't generate a reliable answer. Please try again.",
            )
            return

        if result is not None and getattr(result,'action',None) is not None:
            from llm.responses import natural_llm_enabled
            if not natural_llm_enabled():
                await self._reply_job_error(job,"I couldn't generate a reliable answer. Please try again.")
                return
            executor=getattr(self,'natural_commands',None)
            if executor and job.reply_to is not None:
                await executor.execute_proposal(job.reply_to,result.action)
                return
        if result is None or not result.text:
            await self._reply_job_error(
                job,
                "I couldn't process that image. Please try again in a moment."
                if job.image_attachments
                else "I couldn't generate a reliable answer. Please try again.",
            )
            return

        from action_proposals import action_candidate
        if action_candidate(job.question):
            lang = language_for(job.question,job.assistant_profile)
            await record_for('control',f'natural/unknown/{lang}/llm/unsupported',job.reply_to)
        try:
            await self._reply_mention(job, result.text)
        except Exception:
            await record_for("failure", "llm-reply", job.reply_to)
            raise
        if result.reaction and job.reply_to is not None and not (action_candidate(job.question) and result.reaction == "✅"):
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
                    await record_for("automatic", "mention-reaction", job.reply_to)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
                    logger.info("Could not add mention reaction: %s", exc)
                    await record_for("failure", "mention-reaction", job.reply_to)

        # Persistent memory is synthesized only by the background scheduler. A
        # mention reply must not trigger early extraction or persist raw chat.

    async def _process_memory_job(self, job: MemoryJob) -> bool | None:
        if self.memory is None:
            logger.info("Memory job stopped reason=feature-unavailable")
            return None
        chunks = (
            self.memory.synthesis_chunks(job.batch)
            if hasattr(self.memory, "synthesis_chunks")
            else (job.batch,)
        )
        if not chunks:
            logger.info(
                "Memory job stopped reason=no-chunks scope_id=%s user_id=%s",
                job.batch.scope_id,
                job.batch.user_id,
            )
            return None
        # A cycle author group may contain several chunks. Process only one per
        # scheduler pass so background work cannot pin the CPU continuously.
        batch = chunks[0]
        if not self.memory.can_process_batch(batch):
            logger.info(
                "Memory job stopped reason=batch-invalid-before-inference "
                "scope_id=%s user_id=%s",
                batch.scope_id,
                batch.user_id,
            )
            return None
        entries = self.memory.entries_for_batch(batch)
        logger.info(
            "Memory synthesis prepared scope_id=%s user_id=%s chunks=%d "
            "messages=%d message_chars=%d entries=%d entry_chars=%d",
            batch.scope_id,
            batch.user_id,
            len(chunks),
            len(batch.observations), sum(map(len, batch.observations)),
            len(entries), sum(len(entry["content"]) for entry in entries),
        )
        bot_user = getattr(getattr(self, "client", None), "user", None)
        bot_names = tuple(
            dict.fromkeys(
                name.strip()
                for name in (
                    getattr(bot_user, "display_name", ""),
                    getattr(bot_user, "name", ""),
                )
                if isinstance(name, str) and name.strip()
            )
        )
        result = await asyncio.to_thread(
            generate_memory_delta,
            entries,
            list(batch.observations),
            model=job.model,
            bot_names=bot_names,
        )
        if getattr(result, "skipped", False):
            return None
        if not result.successful:
            await record(
                "failure",
                "memory-batch",
                guild_id=batch.scope_id if batch.scope_id else None,
                scope_type="guild" if batch.scope_id else "dm",
            )
            logger.warning(
                "Memory synthesis failed scope_id=%s user_id=%s observations_retained=%d",
                batch.scope_id,
                batch.user_id,
                len(batch.observations),
            )
            return False
        if not self.memory.can_process_batch(batch):
            logger.info(
                "Memory synthesis discarded reason=batch-invalid-after-inference "
                "scope_id=%s user_id=%s additions=%d corrections=%d",
                batch.scope_id,
                batch.user_id,
                len(result.additions),
                len(result.corrections),
            )
            return None
        committed = self.memory.commit_delta(
            batch, result.additions, result.corrections
        )
        if committed:
            await record(
                "processing",
                "memory-batch",
                guild_id=batch.scope_id if batch.scope_id else None,
                scope_type="guild" if batch.scope_id else "dm",
            )
        else:
            await record(
                "failure",
                "memory-batch",
                guild_id=batch.scope_id if batch.scope_id else None,
                scope_type="guild" if batch.scope_id else "dm",
            )
        logger.info(
            "Memory synthesis commit scope_id=%s user_id=%s committed=%s "
            "additions=%d corrections=%d observations=%d",
            batch.scope_id,
            batch.user_id,
            committed,
            len(result.additions),
            len(result.corrections),
            len(batch.observations),
        )
        return committed

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
            await record_for("automatic", "context-reaction", job.message)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            logger.info("Could not add ordinary-message reaction: %s", exc)
            await record_for("failure", "context-reaction", job.message)

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
        admitted = await self._put_job(
            2,
            ReactionJob(
                message=message,
                model=get_selected_model(),
                channel_id=channel_id,
            ),
        )
        if not admitted:
            self._reaction_pending_channels.discard(channel_id)
            self._reaction_last_attempt.pop(channel_id, None)
            return False
        return True

    def _begin_job_checks(self, user_id: int) -> str | None:
        if user_id in self._user_pending:
            logger.info("Mention request blocked user_id=%s reason=user-pending", user_id)
            return "I'm already working on something for you — hang on."
        remaining = self._cooldown_remaining(user_id)
        if remaining > 0:
            logger.info(
                "Mention request blocked user_id=%s reason=cooldown remaining=%.1fs",
                user_id,
                remaining,
            )
            return f"Please wait **{int(remaining) + 1}s** before trying again."
        return None

    def _mentioned_memories(
        self, message: discord.Message, requester_id: int
    ) -> tuple[tuple[str, str], ...]:
        """Saved profiles of other people @mentioned in the same server."""
        profile_for = getattr(self.memory, "profile_for", None)
        guild = getattr(message, "guild", None)
        if profile_for is None or guild is None:
            return ()
        others = []
        for user in getattr(message, "mentions", []):
            if (
                user.id in (self.bot_id, requester_id)
                or getattr(user, "bot", False)
                or any(user.id == other.id for other in others)
            ):
                continue
            others.append(user)
        memories = []
        for user in others[:MAX_MENTIONED_MEMORIES]:
            profile = profile_for(
                guild_id=guild.id,
                channel_id=message.channel.id,
                user_id=user.id,
                query=message.clean_content,
            )
            if profile:
                memories.append(
                    (user.display_name, trim_profile(profile, MENTIONED_MEMORY_CHAR_BUDGET))
                )
        return tuple(memories)

    def _bot_names(self, guild) -> list[str]:
        """The bot's live server nickname first, then its account names."""
        names = []
        client = getattr(self, "client", None)
        live_display_name = (
            resolve_bot_display_name(guild, client) if client is not None else ""
        )
        if live_display_name:
            names.append(live_display_name)
        client_user = getattr(client, "user", None)
        for value in (
            getattr(client_user, "display_name", ""),
            getattr(client_user, "name", ""),
            getattr(getattr(guild, "me", None), "name", ""),
        ):
            if value and value not in names:
                names.append(value)
        return names

    async def _channel_history(self, channel, bot_name: str, *, before=None) -> list[str]:
        limit = get_llm_context_messages()
        if limit <= 0:
            return []
        lines = []
        async for past_msg in channel.history(limit=limit, before=before):
            lines.append(
                format_chat_line(
                    past_msg.author.display_name,
                    past_msg.clean_content,
                    is_bot=past_msg.author.id == self.bot_id,
                    bot_name=bot_name,
                )
            )
        lines.reverse()
        return lines

    async def _enqueue_job(self, job: AskJob) -> bool:
        if job.user.id in self._user_pending:
            return False
        self._user_pending.add(job.user.id)
        try:
            admitted = await self._put_job(0, job)
        except BaseException:
            self._user_pending.discard(job.user.id)
            raise
        if not admitted:
            self._user_pending.discard(job.user.id)
        return admitted

    async def handle_message(self, message: discord.Message) -> bool:
        text = extract_mention_text(message, self.bot_id, keep_self_mentions=True)
        if text is None:
            return False

        image_attachments, image_count, image_error = (
            select_image_attachments(list(getattr(message, "attachments", [])))
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
            profile=db.get_assistant_profile(user_id)
            lang=language_for(text,profile)
            key="wait" if "wait" in blocked.lower() else "pending" if "already" in blocked.lower() else "busy"
            import re
            seconds=re.search(r"(\d+)s",blocked)
            from action_proposals import action_candidate
            if action_candidate(text):
                await record_for('control',f'natural/unknown/{lang}/llm/model_unavailable',message)
            await message.reply(t(key,lang,seconds=seconds[1] if seconds else 1),mention_author=False)
            return True

        has_image = bool(image_attachments)
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
        assistant_profile = effective_profile(db.get_assistant_profile(user_id))

        bot_names = self._bot_names(message.guild)
        bot_name = bot_names[0] if bot_names else ""
        context_messages = await self._channel_history(
            message.channel, bot_name, before=message
        )

        memory_enabled = False
        user_memory = ""
        memory_batch = None
        memory_history = []
        if not summon_only and self.memory is not None:
            memory_context = self.memory.context_for(
                user_id=user_id,
                guild_id=message.guild.id if message.guild is not None else None,
                channel_id=message.channel.id,
                query=message.clean_content,
            )
            memory_enabled = memory_context.enabled
            user_memory = memory_context.profile
            memory_batch = memory_context.batch
            memory_history = list(getattr(memory_context, "history", ()))

        # Persistent per-user conversation precedes live channel context so
        # the newest live messages win when the shared history budget is tight.
        context_messages = memory_history + context_messages

        mentioned_memories = (
            () if summon_only else self._mentioned_memories(message, user_id)
        )
        user_memory, context_messages = budget_reference_context(
            user_memory,
            context_messages,
            max_chars=(
                VISION_REFERENCE_CONTEXT_CHAR_BUDGET
                if has_image
                else REFERENCE_CONTEXT_CHAR_BUDGET
            )
            - sum(
                len(html.escape(profile, quote=False))
                for _, profile in mentioned_memories
            ),
        )
        logger.info(
            "Mention context prepared user_id=%s memory_enabled=%s memory_chars=%d "
            "history_messages=%d history_chars=%d pending_observations=%d "
            "mentioned_memories=%d",
            user_id,
            memory_enabled,
            len(user_memory),
            len(context_messages),
            sum(map(len, context_messages)),
            len(memory_batch.observations) if memory_batch is not None else 0,
            len(mentioned_memories),
        )

        replied_message = ""
        reference = getattr(message, "reference", None)
        resolved = getattr(reference, "resolved", None)
        if isinstance(resolved, discord.Message):
            replied_content = resolved.clean_content.strip()
            if replied_content:
                replied_message = format_chat_line(
                    resolved.author.display_name,
                    replied_content,
                    is_bot=resolved.author.id == self.bot_id,
                    bot_name=bot_name,
                )

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
            image_attachments=image_attachments,
            replied_message=replied_message,
            bot_names=tuple(bot_names),
            assistant_profile=assistant_profile,
            mentioned_memories=mentioned_memories,
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
        admitted = await self._enqueue_job(job)
        if not admitted:
            await message.reply(
                "The model became busy, so I didn't queue this request. "
                "Please try again shortly.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        notices = []
        if image_count > 1:
            notices.append(
                "I can inspect one image per request, so I'll use the first image I can process."
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
