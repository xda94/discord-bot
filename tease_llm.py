import logging
import os

from ollama_client import DEFAULT_MODEL, OllamaError, query_ollama

logger = logging.getLogger("discord_bot")

TEASE_LLM_ENABLED = os.getenv("TEASE_LLM_ENHANCE", "true").lower() in ("1", "true", "yes")
TEASE_OLLAMA_MODEL = os.getenv("TEASE_OLLAMA_MODEL", DEFAULT_MODEL)
TEASE_OLLAMA_TIMEOUT = int(os.getenv("TEASE_OLLAMA_TIMEOUT", "45"))
TEASE_LLM_MAX_CHARS = 280

MOOD_STYLE: dict[str, str] = {
    "bad": "sarcastic, dismissive, rude in a playful troll-friend way",
    "good": "warm, supportive, and genuinely complimentary",
    "computer": "geeky, terminal-themed, programmer humor and error messages",
    "gen-z": "Gen-Z internet slang, ironic, chronically online",
    "dad": "corny dad jokes, boomer energy, awkward puns",
    "anime": "dramatic anime tropes, over-the-top exclamations",
    "shy": "timid, stuttering, awkward, bashful",
    "lenghel": "obsessed with food and şaormă, casual Romanian eating humor",
}


def build_tease_enhance_prompt(mood: str, line: str) -> str:
    style = MOOD_STYLE.get(mood, mood)
    return f"""Rewrite this Discord tease line in a "{mood}" mood ({style}).

Original line: {line}

Rules:
- Keep the same core meaning, attitude, and person being addressed
- Do not remove or rename the username if one appears in the original
- One short chat message, same language as the original (Romanian stays Romanian)
- More vivid and in-character, but not longer than about 25 words
- Output ONLY the rewritten line — no quotes, labels, or explanation"""


def normalize_tease_response(text: str, *, fallback: str, username: str) -> str:
    cleaned = text.strip().strip("\"'")
    if "\n" in cleaned:
        cleaned = cleaned.split("\n", 1)[0].strip()

    if not cleaned:
        return fallback
    if username and username not in cleaned and username in fallback:
        return fallback
    if len(cleaned) > TEASE_LLM_MAX_CHARS:
        trimmed = cleaned[:TEASE_LLM_MAX_CHARS].rsplit(" ", 1)[0]
        cleaned = trimmed or cleaned[:TEASE_LLM_MAX_CHARS]
    return cleaned


def enhance_tease(mood: str, line: str, *, username: str = "") -> str:
    """Return an LLM-enhanced tease, or the original line on failure."""
    if not TEASE_LLM_ENABLED:
        return line

    try:
        raw = query_ollama(
            build_tease_enhance_prompt(mood, line),
            model=TEASE_OLLAMA_MODEL,
            timeout=TEASE_OLLAMA_TIMEOUT,
        )
        return normalize_tease_response(raw, fallback=line, username=username)
    except OllamaError:
        logger.warning("Tease LLM enhance failed for mood=%s; using original line", mood)
        return line
