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
from PIL import Image, ImageOps

import db
from i18n import t, language_for, localize
from assistant_profiles import AssistantProfile, DEFAULT_PROFILE, effective_profile
from analytics import record_for
from llm.client import (
    LlamaCppError,
    LlamaCppTimeoutError,
    get_allowed_models,
    get_mention_model,
    llama_supports_vision,
)
from llm.capacity import CapacityError, Reservation, reserve
from mention_utils import (
    extract_mention_text,
    resolve_bot_display_name,
    strip_leading_reply_labels,
)
from llm.responses import (
    IMAGE_DESCRIPTION_REQUEST,
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
REFERENCE_CONTEXT_CHAR_BUDGET = 6000
VISION_REFERENCE_CONTEXT_CHAR_BUDGET = 4000
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
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
    max_edge = max(1, int(os.getenv("LLM_IMAGE_MAX_EDGE", "1280")))
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
                resize = max(image.size) > max_edge
                if image_format == "JPEG" and not resize:
                    normalized = data
                    image_mime = "image/jpeg"
                elif image_format == "PNG" and not image.is_animated and not resize:
                    normalized = data
                    image_mime = "image/png"
                else:
                    image = ImageOps.exif_transpose(image)
                    mode = (
                        "RGBA"
                        if "A" in image.getbands() or "transparency" in image.info
                        else "RGB"
                    )
                    image = image.convert(mode)
                    image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                    output = io.BytesIO()
                    image_mime = "image/png"
                    if image_format == "JPEG":
                        image.save(output, format="JPEG", quality=90)
                        image_mime = "image/jpeg"
                    else:
                        image.save(output, format="PNG")
                    normalized = output.getvalue()
                logger.info(
                    "LLM image prepared source_dimensions=%sx%s prepared_dimensions=%sx%s "
                    "source_bytes=%d prepared_bytes=%d resized=%s mime=%s",
                    width, height, *image.size, len(data), len(normalized), resize, image_mime,
                )
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
    return normalized, image_mime


def budget_chat_history(context_messages: list[str], max_chars: int) -> list[str]:
    """Keep the newest messages that fit an escaped-size budget."""
    remaining = max_chars
    selected = []
    for message in reversed(context_messages):
        cost = len(html.escape(message, quote=False))
        if cost <= remaining:
            selected.append(message)
            remaining -= cost
            continue
        if not selected:
            partial = ""
            for character in message:
                remaining -= len(html.escape(character, quote=False))
                if remaining < 0:
                    break
                partial += character
            if partial:
                selected.append(partial)
        break
    return selected[::-1]


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
    image_attachments: tuple[discord.Attachment, ...] = ()
    replied_message: str = ""
    bot_names: tuple[str, ...] = ()
    assistant_profile: AssistantProfile = DEFAULT_PROFILE
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
    ):
        self.client = client
        self.tree = tree
        self.bot_id = bot_id
        self._user_last_ask: dict[int, float] = {}
        self._user_pending: set[int] = set()
        self._worker = SingleSlotWorker(
            self._process_queued_job,
            self._finish_queued_job,
        )
        self._queue = self._worker.queue
        self._queue_sequence = 0
        self._processing = False
        self._worker_task: asyncio.Task | None = None
        self._reaction_pending_channels: set[int] = set()
        self._reaction_last_attempt: dict[int, float] = {}
        self._register_commands()

    def _cooldown_remaining(self, user_id: int) -> float:
        last = self._user_last_ask.get(user_id, 0.0)
        return max(0.0, get_ask_cooldown_seconds() - (time.time() - last))

    def _ensure_worker(self) -> None:
        self._worker.start()
        self._worker_task = self._worker.task

    async def _queue_worker(self) -> None:
        await self._worker.run()

    async def _process_queued_job(self, job) -> WorkerResult:
        self._processing = True
        if isinstance(job, AskJob):
            await self._process_job(job)
            return WorkerResult()
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
        await job.reply_to.reply(
            parts[0],
            mention_author=False,
            allowed_mentions=discord.AllowedMentions(
                users=[job.user], roles=False, everyone=False, replied_user=False
            ),
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
                    "image_bytes": image_bytes,
                    "image_mime": image_mime,
                    "replied_message": job.replied_message,
                    "requester_id": job.user.id,
                    "reply_names": (getattr(job.user, "name", ""), *job.bot_names),
                    "assistant_profile": job.assistant_profile,
                    "bot_name": bot_name,
                }
                result = await asyncio.to_thread(
                    generate_mention_result,
                    job.user.display_name,
                    job.question,
                    **generation_kwargs,
                )
        except LlamaCppTimeoutError as exc:
            await self._reply_job_error(
                job,
                t("image_timeout", language_for(job.question, job.assistant_profile),
                  seconds=exc.timeout_seconds),
            )
            return
        except Exception:
            logger.exception("Unexpected error in mention reply")
            await self._reply_job_error(
                job,
                "I couldn't process that image. Please try again in a moment."
                if job.image_attachments
                else "I couldn't generate a reliable answer. Please try again.",
            )
            return

        if result is None or not result.text:
            await self._reply_job_error(
                job,
                "I couldn't process that image. Please try again in a moment."
                if job.image_attachments
                else "I couldn't generate a reliable answer. Please try again.",
            )
            return

        from llm.responses import action_candidate
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
                model=await asyncio.to_thread(get_selected_model),
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
            profile=await asyncio.to_thread(db.get_assistant_profile,user_id)
            lang=language_for(text,profile)
            key="wait" if "wait" in blocked.lower() else "pending" if "already" in blocked.lower() else "busy"
            import re
            seconds=re.search(r"(\d+)s",blocked)
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

        model = await asyncio.to_thread(get_selected_model)
        assistant_profile = effective_profile(await asyncio.to_thread(db.get_assistant_profile, user_id))

        bot_names = self._bot_names(message.guild)
        bot_name = bot_names[0] if bot_names else ""
        context_messages = await self._channel_history(
            message.channel, bot_name, before=message
        )

        context_messages = budget_chat_history(
            context_messages,
            VISION_REFERENCE_CONTEXT_CHAR_BUDGET
            if has_image
            else REFERENCE_CONTEXT_CHAR_BUDGET,
        )
        logger.info(
            "Mention context prepared user_id=%s history_messages=%d history_chars=%d",
            user_id,
            len(context_messages),
            sum(map(len, context_messages)),
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
            image_attachments=image_attachments,
            replied_message=replied_message,
            bot_names=tuple(bot_names),
            assistant_profile=assistant_profile,
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
            logger.info("Command /llm-set called by %s (model=%s)", interaction.user, model)
            try:
                allowed = get_allowed_models()
                if model not in allowed:
                    await interaction.response.send_message(
                        f"Model `{model}` is not allowed.", ephemeral=True
                    )
                    return
                    
                await asyncio.to_thread(db.set_setting, "mention_model", model)
                await interaction.response.send_message(
                    f"Mention model successfully set to **{model}**.", ephemeral=True
                )
            except Exception:
                logger.exception("Failed to set mention model")
                await interaction.response.send_message(
                    "An error occurred while setting the model.", ephemeral=True
                )
