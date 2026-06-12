import asyncio
import logging
import os

import discord
import requests
from discord import app_commands

logger = logging.getLogger("discord_bot")

DEFAULT_MODEL = "llama3.2:3b"
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "180"))

MODEL_CHOICES: list[tuple[str, str]] = [
    ("Llama 3.2 3B", "llama3.2:3b"),
    ("DeepSeek R1 1.5B", "deepseek-r1:1.5b-qwen-distil-q8_0"),
    ("Qwen3 4B", "qwen3:4b"),
    ("Qwen2.5 Coder 3B", "qwen2.5-coder:3b"),
]
ALLOWED_MODELS = {value for _, value in MODEL_CHOICES}

DISCORD_MESSAGE_LIMIT = 1900


def _chunk_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    return chunks


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
        self._register_commands()

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

            header = f"**{model}**\n"
            chunks = _chunk_message(answer)
            first = header + chunks[0]
            if len(first) > DISCORD_MESSAGE_LIMIT:
                first = chunks[0]
            await interaction.followup.send(first)
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk)
