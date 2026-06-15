import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

import discord
from discord import app_commands

from mention_utils import extract_mention_text
from ollama_client import OllamaError, get_allowed_models, get_default_model, get_mention_model, query_ollama
from tease_llm import generate_mention_reply, generate_summon_reply

logger = logging.getLogger("discord_bot")


def get_ask_cooldown_seconds() -> float:
    return float(os.getenv("ASK_COOLDOWN_SECONDS", "60"))


def get_model_choices() -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=model, value=model)
        for model in get_allowed_models()
    ]


DISCORD_MESSAGE_LIMIT = 2000
DISCORD_SAFE_LIMIT = 1990


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


def format_question_messages(model: str, question: str) -> list[str]:
    return split_discord_messages(question, first_prefix=f"**{model}**\n**Q:** ")


def format_answer_messages(user: discord.abc.User, answer: str) -> list[str]:
    return split_discord_messages(
        answer,
        first_prefix=f"{user.mention}\n**A:** ",
    )


def requests_ahead(*, processing: bool, queue_size: int) -> int:
    return queue_size + (1 if processing else 0)


@dataclass
class AskJob:
    user: discord.abc.User
    question: str
    model: str
    interaction: discord.Interaction | None = None
    channel: discord.abc.Messageable | None = None
    reply_to: discord.Message | None = None
    mention_only: bool = False
    summon_only: bool = False
    question_shown: bool = False
    thinking_message: discord.Message | None = field(default=None, compare=False)


class AskFeature:
    """`/ask` command and @bot mention prompts via Ollama."""

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
                await self._process_job(job)
            except Exception:
                logger.exception("Unhandled error processing /ask job")
                await self._send_error(job, "Something went wrong while asking Ollama.")
            finally:
                self._processing = False
                self._queue.task_done()
                self._user_pending.discard(job.user.id)
                self._user_last_ask[job.user.id] = time.time()

    async def _send_error(self, job: AskJob, text: str) -> None:
        try:
            if job.interaction is not None:
                if job.interaction.response.is_done():
                    await job.interaction.followup.send(text, ephemeral=True)
                else:
                    await job.interaction.response.send_message(text, ephemeral=True)
            elif job.channel is not None:
                await job.channel.send(
                    f"{job.user.mention} {text}",
                    reference=job.reply_to,
                    allowed_mentions=discord.AllowedMentions(users=[job.user]),
                )
        except discord.HTTPException:
            pass

    async def _send_parts(self, job: AskJob, parts: list[str], **kwargs) -> None:
        if not parts:
            return
        if job.interaction is not None:
            await job.interaction.followup.send(parts[0], **kwargs)
            for part in parts[1:]:
                await job.interaction.followup.send(part)
            return
        if job.channel is not None:
            await job.channel.send(
                parts[0],
                reference=job.reply_to,
                **kwargs,
            )
            for part in parts[1:]:
                await job.channel.send(part)

    async def _post_question(self, job: AskJob) -> None:
        messages = format_question_messages(job.model, job.question)
        if job.interaction is not None:
            if job.interaction.response.is_done():
                await job.interaction.followup.send(messages[0])
                for part in messages[1:]:
                    await job.interaction.followup.send(part)
            else:
                await job.interaction.response.send_message(messages[0])
                for part in messages[1:]:
                    await job.interaction.followup.send(part)
        else:
            await self._send_parts(job, messages)

    async def _post_thinking(self, job: AskJob) -> None:
        if job.interaction is not None:
            job.thinking_message = await job.interaction.followup.send(
                "⏳ **Thinking...**"
            )
        elif job.channel is not None:
            job.thinking_message = await job.channel.send("⏳ **Thinking...**")

    async def _clear_thinking(self, job: AskJob) -> None:
        if job.thinking_message is None:
            return
        try:
            await job.thinking_message.delete()
        except discord.HTTPException:
            pass
        job.thinking_message = None

    async def _process_ask_job(self, job: AskJob) -> None:
        if not job.question_shown:
            await self._post_question(job)

        await self._post_thinking(job)
        try:
            answer = await asyncio.to_thread(
                query_ollama, job.question, model=job.model
            )
        except OllamaError as exc:
            await self._clear_thinking(job)
            await self._send_error(job, str(exc))
            return
        except Exception:
            logger.exception("Unexpected error in /ask")
            await self._clear_thinking(job)
            await self._send_error(job, "Something went wrong while asking Ollama.")
            return

        await self._clear_thinking(job)
        allowed = discord.AllowedMentions(users=[job.user])
        await self._send_parts(
            job, format_answer_messages(job.user, answer), allowed_mentions=allowed
        )

    def _begin_job_checks(self, user_id: int, *, for_mention: bool = False) -> str | None:
        if user_id in self._user_pending:
            if for_mention:
                return "I'm already working on something for you — hang on."
            return (
                "You already have a `/ask` in progress or queued. "
                "Wait for it to finish before asking again."
            )
        remaining = self._cooldown_remaining(user_id)
        if remaining > 0:
            return f"Please wait **{int(remaining) + 1}s** before trying again."
        return None

    async def _reply_mention(self, job: AskJob, text: str) -> None:
        parts = split_discord_messages(text)
        if not parts or job.reply_to is None:
            return
        await job.reply_to.reply(parts[0], mention_author=False)
        if job.channel is not None:
            for part in parts[1:]:
                await job.channel.send(part, reference=job.reply_to)

    async def _process_mention_job(self, job: AskJob) -> None:
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
                )
        except Exception:
            logger.exception("Unexpected error in mention reply")
            return

        if not reply:
            return

        await self._reply_mention(job, reply)

    async def _process_job(self, job: AskJob) -> None:
        self._processing = True
        if job.mention_only:
            await self._process_mention_job(job)
        else:
            await self._process_ask_job(job)

    async def _enqueue_job(self, job: AskJob) -> None:
        self._user_pending.add(job.user.id)
        ahead = requests_ahead(
            processing=self._processing, queue_size=self._queue.qsize()
        )

        if job.interaction is not None:
            if ahead > 0:
                await job.interaction.response.send_message(
                    "I'm already thinking on another question. "
                    f"Yours is **#{ahead + 1}** in the queue — I'll answer it when I'm done.",
                    ephemeral=True,
                )
            else:
                await job.interaction.response.defer(thinking=True)
                if not job.summon_only:
                    await self._post_question(job)
                    job.question_shown = True
        elif job.channel is not None and not job.mention_only:
            if ahead > 0:
                await job.channel.send(
                    f"{job.user.mention} I'm already thinking on another question. "
                    f"Yours is **#{ahead + 1}** in the queue.",
                    reference=job.reply_to,
                    allowed_mentions=discord.AllowedMentions(users=[job.user]),
                )
            elif not job.summon_only:
                await self._post_question(job)
                job.question_shown = True
        # mention_only: no preview, no queue notice — reply when ready

        await self._queue.put(job)
        self._ensure_worker()

    async def handle_message(self, message: discord.Message) -> bool:
        text = extract_mention_text(message, self.bot_id)
        if text is None:
            return False

        user_id = message.author.id
        blocked = self._begin_job_checks(user_id, for_mention=True)
        if blocked:
            await message.reply(blocked, mention_author=False)
            return True

        summon_only = not text
        logger.info(
            "Bot mention from %s (summon=%s, len=%s)",
            message.author,
            summon_only,
            len(text),
        )

        job = AskJob(
            user=message.author,
            question=text,
            model=get_mention_model(),
            channel=message.channel,
            reply_to=message,
            mention_only=True,
            summon_only=summon_only,
        )
        await self._enqueue_job(job)
        return True

    def _register_commands(self) -> None:
        default_model = get_default_model()

        @self.tree.command(
            name="ask",
            description="Ask a question to a local Ollama model on the homeserver",
        )
        @app_commands.describe(
            question="Your question or prompt",
            model=f"Ollama model (default: {default_model})",
        )
        @app_commands.choices(model=get_model_choices())
        async def ask(
            interaction: discord.Interaction,
            question: str,
            model: str = default_model,
        ):
            logger.info(
                f"Command /ask called by {interaction.user} "
                f"(model={model}, len={len(question)})"
            )

            blocked = self._begin_job_checks(interaction.user.id)
            if blocked:
                await interaction.response.send_message(blocked, ephemeral=True)
                return

            job = AskJob(
                user=interaction.user,
                question=question,
                model=model,
                interaction=interaction,
            )
            await self._enqueue_job(job)
