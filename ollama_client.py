import logging
import os

import requests

logger = logging.getLogger("discord_bot")

DEFAULT_MODEL = "llama3.2:3b"
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "180"))

ALLOWED_MODELS = {
    "llama3.2:3b",
    "deepseek-r1:1.5b-qwen-distil-q8_0",
    "qwen3:4b",
    "qwen2.5-coder:3b",
}


class OllamaError(Exception):
    pass


def get_ollama_timeout() -> int:
    return int(os.getenv("OLLAMA_TIMEOUT", "180"))


def query_ollama(
    prompt: str,
    model: str = DEFAULT_MODEL,
    *,
    base_url: str = OLLAMA_BASE_URL,
    timeout: int | None = None,
) -> str:
    """Call Ollama /api/generate once, then unload the model (`keep_alive: 0`)."""
    if timeout is None:
        timeout = get_ollama_timeout()
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
        response = requests.post(
            url,
            json=payload,
            timeout=(10, timeout),
        )
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
