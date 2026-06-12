import asyncio
import logging
import os
import time

import discord
import requests
from discord import app_commands

logger = logging.getLogger("discord_bot")

DEFAULT_MODEL = "llama3.2:3b"
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "180"))
ASK_COOLDOWN_SECONDS = float(os.getenv("ASK_COOLDOWN_SECONDS", "60"))

MODEL_CHOICES: list[tuple[str, str]] = [
    ("Llama 3.2 3B", "llama3.2:3b"),
    ("DeepSeek R1 1.5B", "deepseek-r1:1.5b-qwen-distil-q8_0"),
    ("Qwen3 4B", "qwen3:4b"),
    ("Qwen2.5 Coder 3B", "qwen2.5-coder:3b"),
]
ALLOWED_MODELS = {value for _, value in MODEL_CHOICES}

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


def format_ask_messages(model: str, question: str, answer: str) -> list[str]:
    intro = f"**{model}**\n**Q:** {question}\n\n"
    return split_discord_messages(answer, first_prefix=intro)


class OllamaError(Exception):
    pass


def query_ollama(
    prompt: str,
    model: str = DEFAULT_MODEL,
    *,
    base_url: str = OLLAMA_BASE_URL,
    timeout: int = OLLAMA_TIMEOUT,
) -> str:
    """Call Ollama /api/generate once, then unload the model (`keep_alive: 0`)."""
    if model not in ALLOWED_MODELS:
        raise OllamaError(f"Model not allowed: {model}")

    url = f"{base_url}/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": 0,
    }
    try:
        response = requests.post(url, json=payload, timeout=timeout)
    except requests.exceptions.Timeout as exc:
        raise OllamaError(
            f"Ollama did not respond within {timeout}s. "
            "The model may still be loading — try again in a moment."
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        raise OllamaError(
            f"Could not reach Ollama at {base_url}. "
            "Check that Ollama is running and OLLAMA_BASE_URL is set correctly."
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise OllamaError(f"Ollama request failed: {exc}") from exc

    if response.status_code == 404:
        raise OllamaError(
            f"Model `{model}` is not available on Ollama. "
            f"Pull it first: `ollama pull {model}`"
        )
    if not response.ok:
        detail = response.text.strip() or response.reason
        raise OllamaError(f"Ollama returned HTTP {response.status_code}: {detail}")

    try:
        data = response.json()
    except ValueError as exc:
        raise OllamaError("Ollama returned a non-JSON response.") from exc

    answer = (data.get("response") or "").strip()
    if not answer:
        raise OllamaError("Ollama returned an empty response.")
    return answer


class AskFeature:
    """The /ask command — prompts a local Ollama model on demand."""

    def __init__(self, client: discord.Client, tree: app_commands.CommandTree):
        self.client = client
        self.tree = tree
        self._user_last_ask: dict[int, float] = {}
        self._register_commands()

    def _cooldown_remaining(self, user_id: int) -> float:
        last = self._user_last_ask.get(user_id, 0.0)
        return max(0.0, ASK_COOLDOWN_SECONDS - (time.time() - last))

    def _register_commands(self) -> None:
        @self.tree.command(
            name="ask",
            description="Ask a question to a local Ollama model on the homeserver",
        )
        @app_commands.describe(
            question="Your question or prompt",
            model="Ollama model (default: llama3.2:3b)",
        )
        @app_commands.choices(
            model=[app_commands.Choice(name=label, value=value) for label, value in MODEL_CHOICES]
        )
        async def ask(
            interaction: discord.Interaction,
            question: str,
            model: str = DEFAULT_MODEL,
        ):
            logger.info(
                f"Command /ask called by {interaction.user} "
                f"(model={model}, len={len(question)})"
            )

            remaining = self._cooldown_remaining(interaction.user.id)
            if remaining > 0:
                await interaction.response.send_message(
                    f"Please wait **{int(remaining) + 1}s** before using `/ask` again.",
                    ephemeral=True,
                )
                return

            self._user_last_ask[interaction.user.id] = time.time()
            await interaction.response.defer()

            try:
                answer = await asyncio.to_thread(
                    query_ollama, question, model=model
                )
            except OllamaError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            except Exception:
                logger.exception("Unexpected error in /ask")
                await interaction.followup.send(
                    "Something went wrong while asking Ollama.", ephemeral=True
                )
                return

            messages = format_ask_messages(model, question, answer)
            await interaction.followup.send(messages[0])
            for part in messages[1:]:
                await interaction.followup.send(part)
