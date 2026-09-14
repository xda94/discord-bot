from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger("discord_bot")

LLAMA_CPP_BASE_URL = os.getenv(
    "LLAMA_CPP_BASE_URL", "http://127.0.0.1:8080"
).rstrip("/")
LLAMA_CPP_TIMEOUT = int(os.getenv("LLAMA_CPP_TIMEOUT", "180"))


class LlamaCppError(Exception):
    pass


def get_allowed_models() -> tuple[str, ...]:
    raw = os.getenv("LLAMA_CPP_ALLOWED_MODELS")
    if raw is None or not raw.strip():
        raise LlamaCppError(
            "LLAMA_CPP_ALLOWED_MODELS is not set. Add a comma-separated list "
            "of llama-server model aliases to .env."
        )
    models = tuple(model.strip() for model in raw.split(",") if model.strip())
    if not models:
        raise LlamaCppError(
            "LLAMA_CPP_ALLOWED_MODELS is empty. Add at least one model alias to .env."
        )
    return models


def get_default_model() -> str:
    raw = os.getenv("LLAMA_CPP_DEFAULT_MODEL")
    if raw is None or not raw.strip():
        raise LlamaCppError(
            "LLAMA_CPP_DEFAULT_MODEL is not set. Add the llama-server model alias to .env."
        )
    default = raw.strip()
    if default not in set(get_allowed_models()):
        raise LlamaCppError(
            f"LLAMA_CPP_DEFAULT_MODEL {default!r} is not listed in "
            "LLAMA_CPP_ALLOWED_MODELS."
        )
    return default


def get_mention_model() -> str:
    raw = os.getenv("MENTION_LLAMA_CPP_MODEL", "").strip()
    if not raw:
        return get_default_model()
    if raw not in set(get_allowed_models()):
        raise LlamaCppError(
            f"MENTION_LLAMA_CPP_MODEL {raw!r} is not listed in "
            "LLAMA_CPP_ALLOWED_MODELS."
        )
    return raw


def _chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def query_llm(
    prompt: str,
    model: str | None = None,
    *,
    options: dict | None = None,
    base_url: str = LLAMA_CPP_BASE_URL,
    timeout: int | None = None,
) -> str:
    """Call llama-server's OpenAI-compatible chat-completions endpoint.

    The application deliberately sends only a user message. Persistent bot
    identity and behavior belong in the model/chat template configured by the
    llama.cpp deployment, not in an application-provided system message.
    """
    if timeout is None:
        timeout = LLAMA_CPP_TIMEOUT
    if model is None:
        model = get_default_model()

    if model not in set(get_allowed_models()):
        raise LlamaCppError(f"Model not allowed: {model}")

    payload: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }

    if options:
        generation_options = dict(options)
        output_format = generation_options.pop("format", None)
        if output_format == "json":
            payload["response_format"] = {"type": "json_object"}
        payload.update(generation_options)

    headers = {"Content-Type": "application/json"}
    api_key = os.getenv("LLAMA_CPP_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        response = requests.post(
            _chat_completions_url(base_url),
            json=payload,
            headers=headers,
            timeout=(10, timeout),
        )
    except requests.exceptions.Timeout as exc:
        raise LlamaCppError(
            f"llama.cpp did not respond within {timeout}s. "
            "The model may still be loading; try again in a moment."
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        raise LlamaCppError(
            f"Could not reach llama.cpp at {base_url}. Check that llama-server "
            "is running and LLAMA_CPP_BASE_URL is set correctly."
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise LlamaCppError(f"llama.cpp request failed: {exc}") from exc

    if not response.ok:
        detail = response.text.strip() or response.reason
        raise LlamaCppError(
            f"llama.cpp returned HTTP {response.status_code}: {detail}"
        )

    try:
        data = response.json()
    except ValueError as exc:
        raise LlamaCppError("llama.cpp returned a non-JSON response.") from exc

    try:
        answer = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise LlamaCppError("llama.cpp returned an invalid chat-completion response.") from exc

    if not answer:
        raise LlamaCppError("llama.cpp returned an empty response.")
    return answer
