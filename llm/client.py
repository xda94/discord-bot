from __future__ import annotations

import base64
import logging
import os
import time
import uuid

import requests

logger = logging.getLogger("discord_bot")

LLAMA_CPP_BASE_URL = os.getenv(
    "LLAMA_CPP_BASE_URL", "http://127.0.0.1:8080"
).rstrip("/")
LLAMA_CPP_TIMEOUT = int(os.getenv("LLAMA_CPP_TIMEOUT", "180"))
DEFAULT_MAX_TOKENS = 384


class LlamaCppError(Exception):
    pass


def _numeric_metric(mapping: object, key: str) -> int | float | None:
    if not isinstance(mapping, dict):
        return None
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


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


def _server_url(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return f"{base}/{path.lstrip('/')}"


def _request_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    api_key = os.getenv("LLAMA_CPP_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def llama_supports_vision(
    *,
    base_url: str = LLAMA_CPP_BASE_URL,
    timeout: int = 15,
) -> bool:
    """Return llama-server's advertised vision capability."""
    started = time.monotonic()
    logger.info("LLM vision capability check started timeout=%ss", timeout)
    try:
        response = requests.get(
            _server_url(base_url, "/props"),
            headers=_request_headers(),
            timeout=(10, timeout),
        )
    except requests.exceptions.Timeout as exc:
        logger.warning(
            "LLM vision capability check timed out elapsed=%.2fs",
            time.monotonic() - started,
        )
        raise LlamaCppError(
            "llama.cpp did not respond while checking vision support."
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        logger.warning(
            "LLM vision capability connection failed elapsed=%.2fs error=%s",
            time.monotonic() - started,
            type(exc).__name__,
        )
        raise LlamaCppError(
            f"Could not reach llama.cpp at {base_url} while checking vision support."
        ) from exc
    except requests.exceptions.RequestException as exc:
        logger.warning(
            "LLM vision capability request failed elapsed=%.2fs error=%s",
            time.monotonic() - started,
            type(exc).__name__,
        )
        raise LlamaCppError(f"Could not check llama.cpp vision support: {exc}") from exc

    if not response.ok:
        logger.warning(
            "LLM vision capability check returned status=%s elapsed=%.2fs",
            response.status_code,
            time.monotonic() - started,
        )
        raise LlamaCppError(
            f"llama.cpp vision check returned HTTP {response.status_code} "
            f"({response.reason})."
        )
    try:
        data = response.json()
        enabled = data["modalities"]["vision"] is True
        logger.info(
            "LLM vision capability check completed enabled=%s elapsed=%.2fs",
            enabled,
            time.monotonic() - started,
        )
        return enabled
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning(
            "LLM vision capability response invalid status=%s elapsed=%.2fs",
            response.status_code,
            time.monotonic() - started,
        )
        raise LlamaCppError(
            "llama.cpp returned an invalid vision-capability response."
        ) from exc


def query_llm(
    prompt: str,
    model: str | None = None,
    *,
    options: dict | None = None,
    response_schema: dict | None = None,
    image_bytes: bytes | None = None,
    image_mime: str | None = None,
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

    if (image_bytes is None) != (image_mime is None):
        raise LlamaCppError("Image bytes and MIME type must be provided together.")

    message_content: str | list[dict]
    if image_bytes is None:
        message_content = prompt
    else:
        if image_mime not in {"image/png", "image/jpeg"}:
            raise LlamaCppError(f"Unsupported image MIME type: {image_mime}")
        encoded = base64.b64encode(image_bytes).decode("ascii")
        message_content = [
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image_mime};base64,{encoded}",
                },
            },
            {"type": "text", "text": prompt},
        ]

    payload: dict = {
        "model": model,
        "messages": [{"role": "user", "content": message_content}],
        "stream": False,
        "max_tokens": DEFAULT_MAX_TOKENS,
    }

    if options:
        generation_options = dict(options)
        output_format = generation_options.pop("format", None)
        if output_format == "json":
            response_format = {"type": "json_object"}
            if response_schema is not None:
                response_format["schema"] = response_schema
            payload["response_format"] = response_format
        payload.update(generation_options)
    elif response_schema is not None:
        payload["response_format"] = {
            "type": "json_object",
            "schema": response_schema,
        }

    max_tokens = payload["max_tokens"]
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
        raise LlamaCppError("max_tokens must be a positive integer.")

    request_id = uuid.uuid4().hex[:8]
    started = time.monotonic()
    logger.info(
        "LLM request started request=%s model=%s prompt_chars=%d max_tokens=%d "
        "timeout=%ss image=%s image_bytes=%d json_schema=%s",
        request_id,
        model,
        len(prompt),
        max_tokens,
        timeout,
        image_bytes is not None,
        len(image_bytes) if image_bytes is not None else 0,
        response_schema is not None,
    )
    try:
        response = requests.post(
            _chat_completions_url(base_url),
            json=payload,
            headers=_request_headers(),
            timeout=(10, timeout),
        )
    except requests.exceptions.Timeout as exc:
        logger.warning(
            "LLM request timed out request=%s model=%s timeout=%ss elapsed=%.2fs",
            request_id,
            model,
            timeout,
            time.monotonic() - started,
        )
        raise LlamaCppError(
            f"llama.cpp did not respond within {timeout}s. "
            "The model may still be loading; try again in a moment."
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        logger.warning(
            "LLM connection failed request=%s model=%s elapsed=%.2fs error=%s",
            request_id,
            model,
            time.monotonic() - started,
            type(exc).__name__,
        )
        raise LlamaCppError(
            f"Could not reach llama.cpp at {base_url}. Check that llama-server "
            "is running and LLAMA_CPP_BASE_URL is set correctly."
        ) from exc
    except requests.exceptions.RequestException as exc:
        logger.warning(
            "LLM HTTP request failed request=%s model=%s elapsed=%.2fs error=%s",
            request_id,
            model,
            time.monotonic() - started,
            type(exc).__name__,
        )
        raise LlamaCppError(f"llama.cpp request failed: {exc}") from exc
    finally:
        logger.info(
            "LLM HTTP wait ended request=%s model=%s elapsed=%.2fs",
            request_id,
            model,
            time.monotonic() - started,
        )

    if not response.ok:
        logger.warning(
            "LLM HTTP error request=%s model=%s status=%s response_chars=%d elapsed=%.2fs",
            request_id,
            model,
            response.status_code,
            len(response.text),
            time.monotonic() - started,
        )
        raise LlamaCppError(
            f"llama.cpp returned HTTP {response.status_code} ({response.reason})."
        )

    try:
        data = response.json()
    except ValueError as exc:
        logger.warning(
            "LLM response was not JSON request=%s model=%s status=%s response_chars=%d",
            request_id,
            model,
            response.status_code,
            len(response.text),
        )
        raise LlamaCppError("llama.cpp returned a non-JSON response.") from exc

    try:
        choice = data["choices"][0]
        # Never acknowledge memory observations from a truncated extraction,
        # even if the partial response happens to be valid JSON.
        if choice.get("finish_reason") == "length":
            logger.warning(
                "LLM response hit token limit request=%s model=%s max_tokens=%d",
                request_id,
                model,
                max_tokens,
            )
            raise LlamaCppError("llama.cpp exhausted the output token budget.")
        answer = choice["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        logger.warning(
            "LLM response shape invalid request=%s model=%s response_chars=%d",
            request_id,
            model,
            len(response.text),
        )
        raise LlamaCppError("llama.cpp returned an invalid chat-completion response.") from exc

    if not answer:
        logger.warning("LLM response empty request=%s model=%s", request_id, model)
        raise LlamaCppError("llama.cpp returned an empty response.")
    usage = data.get("usage") if isinstance(data, dict) else None
    timings = data.get("timings") if isinstance(data, dict) else None
    prompt_details = usage.get("prompt_tokens_details") if isinstance(usage, dict) else None
    logger.info(
        "LLM request completed request=%s model=%s finish=%s response_chars=%d "
        "prompt_tokens=%s cached_tokens=%s completion_tokens=%s total_tokens=%s "
        "cache_tokens=%s prompt_ms=%s predicted_ms=%s prompt_tps=%s predicted_tps=%s "
        "elapsed=%.2fs",
        request_id,
        model,
        choice.get("finish_reason"),
        len(answer),
        _numeric_metric(usage, "prompt_tokens"),
        _numeric_metric(prompt_details, "cached_tokens"),
        _numeric_metric(usage, "completion_tokens"),
        _numeric_metric(usage, "total_tokens"),
        _numeric_metric(timings, "cache_n"),
        _numeric_metric(timings, "prompt_ms"),
        _numeric_metric(timings, "predicted_ms"),
        _numeric_metric(timings, "prompt_per_second"),
        _numeric_metric(timings, "predicted_per_second"),
        time.monotonic() - started,
    )
    return answer
