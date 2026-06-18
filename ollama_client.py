from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger("discord_bot")

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "180"))


class OllamaError(Exception):
    pass


def get_allowed_models() -> tuple[str, ...]:
    raw = os.getenv("OLLAMA_ALLOWED_MODELS")
    if raw is None or not raw.strip():
        raise OllamaError(
            "OLLAMA_ALLOWED_MODELS is not set. Add a comma-separated list to .env."
        )
    models = tuple(m.strip() for m in raw.split(",") if m.strip())
    if not models:
        raise OllamaError(
            "OLLAMA_ALLOWED_MODELS is empty. Add at least one model to .env."
        )
    return models


def get_default_model() -> str:
    raw = os.getenv("OLLAMA_DEFAULT_MODEL")
    if raw is None or not raw.strip():
        raise OllamaError(
            "OLLAMA_DEFAULT_MODEL is not set. Add it to .env."
        )
    default = raw.strip()
    if default not in set(get_allowed_models()):
        raise OllamaError(
            f"OLLAMA_DEFAULT_MODEL {default!r} is not listed in OLLAMA_ALLOWED_MODELS."
        )
    return default


def get_mention_model() -> str:
    raw = os.getenv("MENTION_OLLAMA_MODEL", "").strip()
    if not raw:
        return get_default_model()
    if raw not in set(get_allowed_models()):
        raise OllamaError(
            f"MENTION_OLLAMA_MODEL {raw!r} is not listed in OLLAMA_ALLOWED_MODELS."
        )
    return raw


def _resolve_model(model: str | None) -> str:
    if model is None:
        model = get_default_model()
    if model not in set(get_allowed_models()):
        raise OllamaError(f"Model not allowed: {model}")
    return model


def _post_ollama(
    path: str,
    payload: dict,
    *,
    base_url: str,
    timeout: int,
    model: str,
) -> dict:
    """POST to an Ollama endpoint, handling transport and HTTP errors uniformly."""
    url = f"{base_url}{path}"
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
        return response.json()
    except ValueError as exc:
        raise OllamaError("Ollama returned a non-JSON response.") from exc


def query_ollama(
    prompt: str,
    model: str | None = None,
    system: str | None = None,
    *,
    base_url: str = OLLAMA_BASE_URL,
    timeout: int | None = None,
) -> str:
    """Call Ollama /api/generate once, then unload the model (`keep_alive: 0`)."""
    if timeout is None:
        timeout = OLLAMA_TIMEOUT
    model = _resolve_model(model)

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "keep_alive": 0,
    }
    if system is not None:
        payload["system"] = system

    data = _post_ollama(
        "/api/generate", payload, base_url=base_url, timeout=timeout, model=model
    )
    answer = (data.get("response") or "").strip()
    if not answer:
        raise OllamaError("Ollama returned an empty response.")
    return answer


def chat_ollama(
    messages: list[dict[str, str]],
    model: str | None = None,
    *,
    base_url: str = OLLAMA_BASE_URL,
    timeout: int | None = None,
) -> str:
    """Call Ollama /api/chat once, then unload the model (`keep_alive: 0`).

    Stateless like /api/generate — the full `messages` list is sent every call.
    Using the chat endpoint appends the model's assistant-turn generation marker
    after the final message, which stops it from continuing the transcript.
    """
    if timeout is None:
        timeout = OLLAMA_TIMEOUT
    model = _resolve_model(model)

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": 0,
    }

    data = _post_ollama(
        "/api/chat", payload, base_url=base_url, timeout=timeout, model=model
    )
    message = data.get("message") or {}
    answer = (message.get("content") or "").strip()
    if not answer:
        raise OllamaError("Ollama returned an empty response.")
    return answer
