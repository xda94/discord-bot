from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from dataclasses import dataclass, field

import discord
from discord import app_commands

import db
from features.llm_feedback import LLMFeedbackFeature
from features.user_memory import MEMORY_MAX_CHARS, MemoryBatch, UserMemoryFeature
from llm_client import (
    LlamaCppError,
    get_allowed_models,
    get_mention_model,
    llama_supports_vision,
)
from mention_utils import extract_mention_text
from tease_llm import (
    generate_memory_update,
    generate_mention_reply,
    generate_summon_reply,
)

logger = logging.getLogger("discord_bot")


def get_ask_cooldown_seconds() -> float:
    return float(os.getenv("ASK_COOLDOWN_SECONDS", "60"))


def get_llm_context_messages() -> int:
    return int(os.getenv("LLM_CONTEXT_MESSAGES", "0"))


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
MENTION_PROMPT_VERSION = "mention-v1"
MEMORY_MENTION_PROMPT_VERSION = "mention-v2-memory"
VISION_MENTION_PROMPT_VERSION = "mention-v3-vision"
VISION_MEMORY_MENTION_PROMPT_VERSION = "mention-v4-vision-memory"
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
    """Prioritize the compact profile, then keep the newest channel context."""
    def escaped_prefix(value: str, budget: int) -> tuple[str, int]:
        used = 0
        end = 0
        for end, character in enumerate(value, start=1):
            cost = len(html.escape(character, quote=False))
            if used + cost > budget:
                return value[:end - 1], used
            used += cost
        return value[:end], used

    memory, memory_cost = escaped_prefix(user_memory, max_chars)
    remaining = max(0, max_chars - memory_cost)
    selected_reversed: list[str] = []
    for message in reversed(context_messages):
        if remaining <= 0:
            break
        escaped_cost = len(html.escape(message, quote=False))
        if escaped_cost <= remaining:
            selected_reversed.append(message)
            remaining -= escaped_cost
            continue
        # Preserve at least the newest partial message when the remaining
        # budget cannot fit it in full. The author label is at the beginning.
        if not selected_reversed:
            partial, _ = escaped_prefix(message, remaining)
            if partial:
                selected_reversed.append(partial)
        break
    return memory, list(reversed(selected_reversed))


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
    memory_profile: str = ""
    memory_enabled: bool = False
    memory_batch: MemoryBatch | None = None
    image_attachment: discord.Attachment | None = None
    image_mime: str | None = None
    prompt_version: str = MENTION_PROMPT_VERSION


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
        self._queue: asyncio.Queue[AskJob] = asyncio.Queue()
        self._processing = False
        self._worker_task: asyncio.Task | None = None
        self._register_commands()

    def _cooldown_remaining(self, user_id: int) -> float:
        last = self._user_last_ask.get(user_id, 0.0)
        return max(0.0, get_ask_cooldown_seconds() - (time.time() - last))

    def _ensure_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._queue_worker())

    async def _queue_worker(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                self._processing = True
                await self._process_job(job)
            except Exception:
                logger.exception("Unhandled error processing mention job")
            finally:
                self._processing = False
                self._queue.task_done()
                self._user_pending.discard(job.user.id)
                self._user_last_ask[job.user.id] = time.time()

    async def _reply_mention(self, job: AskJob, text: str) -> None:
        if not text.strip() or job.reply_to is None:
            return
        # Discord mentions require the user's ID. A model-generated display
        # name (e.g. "Robeeque:") is only text, so add the mention ourselves.
        text = text.strip()
        name_label = f"{job.user.display_name}:"
        if text.startswith(name_label):
            text = text[len(name_label):].lstrip()
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
            else:
                reply = await asyncio.to_thread(
                    generate_mention_reply,
                    job.user.display_name,
                    job.question,
                    model=job.model,
                    context_messages=job.context_messages,
                    user_memory=job.user_memory,
                    memory_enabled=job.memory_enabled,
                    image_bytes=image_bytes,
                    image_mime=job.image_mime,
                )
        except Exception:
            logger.exception("Unexpected error in mention reply")
            if job.image_attachment is not None:
                await self._reply_job_error(
                    job, "I couldn't process that image. Please try again in a moment."
                )
            return

        if not reply:
            if job.image_attachment is not None:
                await self._reply_job_error(
                    job, "I couldn't process that image. Please try again in a moment."
                )
            return

        await self._reply_mention(job, reply)

        # The user-facing answer is deliberately sent before this second model
        # call. A consolidation failure never suppresses or delays delivery of
        # a reply that was already generated successfully.
        if (
            not job.summon_only
            and self.memory is not None
            and job.memory_batch is not None
            and self.memory.can_process_batch(job.memory_batch)
        ):
            result = await asyncio.to_thread(
                generate_memory_update,
                job.memory_profile,
                list(job.memory_batch.observations),
                model=job.model,
                max_chars=MEMORY_MAX_CHARS,
            )
            if result.successful:
                self.memory.commit_batch(job.memory_batch, result.profile)

    def _begin_job_checks(self, user_id: int) -> str | None:
        if user_id in self._user_pending:
            return "I'm already working on something for you — hang on."
        remaining = self._cooldown_remaining(user_id)
        if remaining > 0:
            return f"Please wait **{int(remaining) + 1}s** before trying again."
        return None

    async def _enqueue_job(self, job: AskJob) -> None:
        self._user_pending.add(job.user.id)
        await self._queue.put(job)
        self._ensure_worker()

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
        memory_profile = ""
        memory_batch = None
        if not summon_only and self.memory is not None:
            memory_context = self.memory.context_for(message)
            memory_enabled = memory_context.enabled
            memory_profile = memory_context.profile
            user_memory = memory_profile
            memory_batch = memory_context.batch

        user_memory, context_messages = budget_reference_context(
            user_memory,
            context_messages,
            max_chars=(
                VISION_REFERENCE_CONTEXT_CHAR_BUDGET
                if has_image
                else REFERENCE_CONTEXT_CHAR_BUDGET
            ),
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
            memory_profile=memory_profile,
            memory_enabled=memory_enabled,
            memory_batch=memory_batch,
            image_attachment=image_attachment,
            image_mime=image_mime,
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
