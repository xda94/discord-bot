from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

import discord
from discord import app_commands

import db
from mention_utils import extract_mention_text, resolve_bot_display_name
from ollama_client import OllamaError, get_allowed_models, get_mention_model
from tease_llm import generate_mention_reply, generate_summon_reply

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
    bot_name: str = ""


class LLMMentionFeature:
    """@bot mention prompts via Ollama and /llm_set command."""

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
        parts = split_discord_messages(text)
        if not parts or job.reply_to is None:
            return
        await job.reply_to.reply(parts[0], mention_author=False)
        if job.channel is not None:
            for part in parts[1:]:
                await job.channel.send(part, reference=job.reply_to)

    async def _process_job(self, job: AskJob) -> None:
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
                    bot_name=job.bot_name,
                )
        except Exception:
            logger.exception("Unexpected error in mention reply")
            return

        if not reply:
            return

        await self._reply_mention(job, reply)

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

        user_id = message.author.id
        blocked = self._begin_job_checks(user_id)
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
        
        # Get dynamic mention model from DB or fallback
        model = db.get_setting("mention_model") or get_mention_model()

        limit = get_llm_context_messages()
        context_messages = []
        if limit > 0:
            async for past_msg in message.channel.history(limit=limit, before=message):
                context_messages.append(f"{past_msg.author.display_name}: {past_msg.clean_content}")
            context_messages.reverse()

        job = AskJob(
            user=message.author,
            question=text,
            model=model,
            channel=message.channel,
            reply_to=message,
            summon_only=summon_only,
            context_messages=context_messages,
            bot_name=resolve_bot_display_name(message.guild, self.client),
        )
        await self._enqueue_job(job)
        return True

    def _register_commands(self) -> None:
        @self.tree.command(
            name="llm_set",
            description="Set the model used when the bot is mentioned",
        )
        @app_commands.describe(
            model="Ollama model to use for mentions",
        )
        @app_commands.choices(model=get_model_choices())
        async def llm_set(
            interaction: discord.Interaction,
            model: str,
        ):
            logger.info(f"Command /llm_set called by {interaction.user} (model={model})")
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
