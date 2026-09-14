from __future__ import annotations

import logging
import os

from llm_client import LlamaCppError, get_default_model, get_mention_model, query_llm

logger = logging.getLogger("discord_bot")

TEASE_LLM_ENABLED = os.getenv("TEASE_LLM_ENHANCE", "true").lower() in ("1", "true", "yes")
TEASE_LLAMA_CPP_TIMEOUT = int(os.getenv("TEASE_LLAMA_CPP_TIMEOUT", "45"))
TEASE_LLM_MAX_CHARS = 280

MOOD_STYLE: dict[str, str] = {
    "bad": "sarcastic, dismissive, rude in a playful troll-friend way, and a little mean-spirited, with a touch of irony",
    "good": "warm, supportive, and genuinely complimentary, with a touch of humor, and a touch of wholesome positivity",
    "computer": "geeky, terminal-themed, programmer humor and error messages, and a little robotic, with a touch of nerdy internet culture",
    "gen-z": "Gen-Z internet slang, ironic, chronically online, use a lot of emojis, and be a little chaotic",
    "dad": "corny dad jokes, boomer energy, awkward puns, and wholesome humor, and a touch of self-deprecation",
    "anime": "dramatic anime tropes, over-the-top exclamations, and exaggerated emotions, and a touch of Japanese internet culture",
    "shy": "timid, stuttering, awkward, bashful, and hesitant, with a touch of nervousness, and a little self-conscious",
    "lenghel": "obsessed with food and şaormă, casual Romanian eating humor, and a little bit of Romanian internet slang, and a touch of Romanian internet culture",
}


def get_tease_model() -> str:
    override = os.getenv("TEASE_LLAMA_CPP_MODEL", "").strip()
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


def build_inactivity_prompt(bot_name: str | None, ask_question: bool) -> str:
    bot_context = f" The bot's display name is {bot_name}." if bot_name else ""
    base = (
        "Generate an original Discord message for a server that has been silent "
        f"for a whole day.{bot_context} Break the silence in a playful, slightly "
        "cheeky way that feels natural rather than like a stock phrase."
    )
    if ask_question:
        task = (
            " Address one person directly and invent a casual, fun question that "
            "could get them talking. Do not use a name; talk to them directly."
        )
    else:
        task = " Creatively react to the silence and invite the channel to respond."
    return base + task + (
        "\n\nRules:\n"
        "- One short line, max 25 words.\n"
        "- Casual, internet tone.\n"
        "- Choose the subject and wording yourself; avoid canned catchphrases.\n"
        "- Output ONLY the message text. No quotes, labels, or preamble."
    )


def build_mention_prompt(
    username: str,
    content: str,
    context_messages: list[str] | None = None,
) -> str:
    """Build the user message without injecting an application system prompt."""
    prompt = ""
    if context_messages:
        prompt += "<chat_history>\n"
        for msg in context_messages:
            prompt += f"{msg}\n"
        prompt += "</chat_history>\n\n"

    prompt += f'<message from="{username}">\n{content}\n</message>\n\n'
    prompt += "Your reply:"

    return prompt


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
        raw = query_llm(
            build_tease_prompt(mood, username, context),
            model=get_tease_model(),
            timeout=TEASE_LLAMA_CPP_TIMEOUT,
        )
        result = normalize_tease_response(raw)
        return result or None
    except LlamaCppError:
        logger.warning("Tease LLM generation failed for mood=%s", mood)
        return None


def generate_mention_reply(
    username: str,
    content: str,
    *,
    model: str | None = None,
    context_messages: list[str] | None = None,
) -> str | None:
    """Direct LLM reply when the bot is @mentioned with a message."""
    if model is None:
        model = get_mention_model()
    try:
        user_prompt = build_mention_prompt(username, content, context_messages)
        raw = query_llm(
            prompt=user_prompt,
            model=model,
        )
        result = normalize_llm_reply(raw)
        return result or None
    except LlamaCppError:
        logger.warning("Mention LLM generation failed for user=%s", username)
        return None


def generate_summon_reply(username: str, *, model: str | None = None) -> str | None:
    """LLM reply when the bot is pinged with no message."""
    if model is None:
        model = get_mention_model()
    try:
        raw = query_llm(
            build_summon_prompt(username),
            model=model,
            timeout=TEASE_LLAMA_CPP_TIMEOUT,
        )
        result = normalize_tease_response(raw)
        return result or None
    except LlamaCppError:
        logger.warning("Summon LLM generation failed for user=%s", username)
        return None


def generate_inactivity_message(
    bot_name: str | None = None, *, ask_question: bool, model: str | None = None
) -> str | None:
    """LLM-generated nudge for a silent channel, or None if it failed.

    The caller adds the ping; this function only produces the message text.
    """
    try:
        raw = query_llm(
            build_inactivity_prompt(bot_name, ask_question),
            model=model,
            timeout=TEASE_LLAMA_CPP_TIMEOUT,
            options={"temperature": 0.8},
        )
        return normalize_tease_response(raw) or None
    except LlamaCppError:
        logger.warning("Inactivity LLM generation failed")
        return None
