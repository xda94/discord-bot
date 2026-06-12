import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

import discord
from discord import app_commands

from ollama_client import OllamaError, get_allowed_models, get_default_model, query_ollama

logger = logging.getLogger("discord_bot")


def get_ask_cooldown_seconds() -> float:
    return float(os.getenv("ASK_COOLDOWN_SECONDS", "60"))



def get_model_choices() -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=model, value=model)
        for model in get_allowed_models()
    ]

# Discord hard limit is 2000; stay below it for markdown / invisible overhead.
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
            # Prefix alone is too long (shouldn't happen for model names).
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
    """How many /ask jobs must finish before a newly queued one starts."""
    return queue_size + (1 if processing else 0)


@dataclass
class AskJob:
    interaction: discord.Interaction
    question: str
    model: str
    question_shown: bool = field(default=False)
    thinking_message: discord.Message | None = field(default=None, compare=False)


class AskFeature:
    """The /ask command — prompts a local Ollama model on demand."""

    def __init__(self, client: discord.Client, tree: app_commands.CommandTree):
        self.client = client
        self.tree = tree
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
                try:
                    await job.interaction.followup.send(
                        "Something went wrong while asking Ollama.", ephemeral=True
                    )
                except discord.HTTPException:
                    pass
            finally:
                self._processing = False
                self._queue.task_done()
                user_id = job.interaction.user.id
                self._user_pending.discard(user_id)
                self._user_last_ask[user_id] = time.time()

    async def _post_question(self, job: AskJob) -> None:
        messages = format_question_messages(job.model, job.question)
        if job.interaction.response.is_done():
            await job.interaction.followup.send(messages[0])
            for part in messages[1:]:
                await job.interaction.followup.send(part)
        else:
            await job.interaction.response.send_message(messages[0])
            for part in messages[1:]:
                await job.interaction.followup.send(part)

    async def _post_thinking(self, job: AskJob) -> None:
        job.thinking_message = await job.interaction.followup.send("⏳ **Thinking...**")

    async def _clear_thinking(self, job: AskJob) -> None:
        if job.thinking_message is None:
            return
        try:
            await job.thinking_message.delete()
        except discord.HTTPException:
            pass
        job.thinking_message = None

    async def _process_job(self, job: AskJob) -> None:
        self._processing = True
        if not job.question_shown:
            await self._post_question(job)

        await self._post_thinking(job)
        try:
            answer = await asyncio.to_thread(
                query_ollama, job.question, model=job.model
            )
        except OllamaError as exc:
            await self._clear_thinking(job)
            await job.interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception:
            logger.exception("Unexpected error in /ask")
            await self._clear_thinking(job)
            await job.interaction.followup.send(
                "Something went wrong while asking Ollama.", ephemeral=True
            )
            return

        await self._clear_thinking(job)
        allowed = discord.AllowedMentions(users=[job.interaction.user])
        parts = format_answer_messages(job.interaction.user, answer)
        await job.interaction.followup.send(parts[0], allowed_mentions=allowed)
        for part in parts[1:]:
            await job.interaction.followup.send(part)

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

            user_id = interaction.user.id
            if user_id in self._user_pending:
                await interaction.response.send_message(
                    "You already have a `/ask` in progress or queued. "
                    "Wait for it to finish before asking again.",
                    ephemeral=True,
                )
                return

            remaining = self._cooldown_remaining(user_id)
            if remaining > 0:
                await interaction.response.send_message(
                    f"Please wait **{int(remaining) + 1}s** before using `/ask` again.",
                    ephemeral=True,
                )
                return

            self._user_pending.add(user_id)

            ahead = requests_ahead(
                processing=self._processing, queue_size=self._queue.qsize()
            )
            job = AskJob(interaction=interaction, question=question, model=model)

            if ahead > 0:
                await interaction.response.send_message(
                    "I'm already thinking on another question. "
                    f"Yours is **#{ahead + 1}** in the queue — I'll answer it when I'm done.",
                    ephemeral=True,
                )
            else:
                await interaction.response.defer(thinking=True)
                await self._post_question(job)
                job.question_shown = True

            await self._queue.put(job)
            self._ensure_worker()
