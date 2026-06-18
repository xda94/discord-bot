from __future__ import annotations

import logging
import os

from ollama_client import (
    OllamaError,
    chat_ollama,
    get_default_model,
    get_mention_model,
    query_ollama,
)

logger = logging.getLogger("discord_bot")

TEASE_LLM_ENABLED = os.getenv("TEASE_LLM_ENHANCE", "true").lower() in ("1", "true", "yes")
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


def get_tease_model() -> str:
    override = os.getenv("TEASE_OLLAMA_MODEL", "").strip()
    if override:
        return override
    return get_default_model()


def build_tease_prompt(mood: str, username: str, context: str) -> str:
    style = MOOD_STYLE.get(mood, mood)
    return f"""Act as a Discord bot. A user named '{username}' just said: "{context}"
Write a short, one-line response to them in a "{mood}" mood ({style}).

Rules:
- Max 25 words.
- Same language as the user (Romanian stays Romanian).
- Output ONLY the response text. No quotes, labels, or preamble."""


def build_summon_prompt(username: str) -> str:
    return f"""You are a Discord bot. A user named '{username}' just pinged you with no message.
Reply in one short message: acknowledge they called you, and ask what they need.
Keep it casual. Output ONLY the reply."""


def build_mention_messages(
    username: str, content: str, context_messages: list[str] | None = None
) -> list[dict[str, str]]:
    """Build the /api/chat `messages` list for an @mention reply.

    The channel is a busy multi-party room, so prior messages are NOT mapped to
    chat roles (there is no single "user"). Instead they go into one labelled
    context block, leaving the message that tagged the bot as the only real user
    turn — so the model's generation marker lands right after it and it replies
    instead of continuing the transcript.
    """
    system = (
        "You are Balen, a helpful conversational Discord bot in a busy channel "
        "where many different people talk. You reply only to the single message "
        "that is directed at you. Any recent channel messages are background "
        "context from other people — use them only if relevant, and never repeat, "
        "quote, or echo them. Respond with a single original reply in your own "
        "words. Match the user's language. Output ONLY your reply text, with no "
        "names, labels, tags, or quotes."
    )

    if context_messages:
        history = "\n".join(context_messages)
        user_content = (
            "Recent channel messages (background context only — do not repeat these):\n"
            f"{history}\n"
            "---\n"
            "Now write your reply to this message, which is the one directed at you:\n"
            f"{username}: {content}"
        )
    else:
        user_content = f"{username}: {content}"

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]


def normalize_llm_reply(text: str, *, max_chars: int | None = None) -> str:
    cleaned = text.strip().strip("\"'").strip()
    if max_chars is not None and len(cleaned) > max_chars:
        trimmed = cleaned[:max_chars].rsplit(" ", 1)[0]
        cleaned = trimmed or cleaned[:max_chars]
    return cleaned


def normalize_tease_response(text: str) -> str:
    return normalize_llm_reply(text, max_chars=TEASE_LLM_MAX_CHARS)


def enhance_tease(mood: str, username: str, context: str) -> str | None:
    """Generate a tease from mood and message context, or None if disabled/failed."""
    if not TEASE_LLM_ENABLED:
        return None

    try:
        raw = query_ollama(
            build_tease_prompt(mood, username, context),
            model=get_tease_model(),
            timeout=TEASE_OLLAMA_TIMEOUT,
        )
        result = normalize_tease_response(raw)
        return result or None
    except OllamaError:
        logger.warning("Tease LLM generation failed for mood=%s", mood)
        return None


def generate_mention_reply(
    username: str, content: str, *, model: str | None = None, context_messages: list[str] | None = None
) -> str | None:
    """Direct LLM reply when the bot is @mentioned with a message."""
    if model is None:
        model = get_mention_model()
    try:
        messages = build_mention_messages(username, content, context_messages)
        raw = chat_ollama(messages, model=model)
        result = normalize_llm_reply(raw)
        return result or None
    except OllamaError:
        logger.warning("Mention LLM generation failed for user=%s", username)
        return None


def generate_summon_reply(username: str, *, model: str | None = None) -> str | None:
    """LLM reply when the bot is pinged with no message."""
    if model is None:
        model = get_mention_model()
    try:
        raw = query_ollama(
            build_summon_prompt(username),
            model=model,
            timeout=TEASE_OLLAMA_TIMEOUT,
        )
        result = normalize_tease_response(raw)
        return result or None
    except OllamaError:
        logger.warning("Summon LLM generation failed for user=%s", username)
        return None
