from __future__ import annotations

import logging
import os
import random

from ollama_client import OllamaError, get_default_model, get_mention_model, query_ollama

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


INACTIVITY_TOPICS = [
    "what they are eating or drinking",
    "the most chaotic thing they did today",
    "what they are currently procrastinating on",
    "a terrible movie or show recommendation",
    "their current life status or how they are surviving",
    "their plans for the weekend or rest of the day",
    "an extremely controversial but low-stakes opinion (e.g. pineapple on pizza)",
    "what project they are supposed to be working on right now",
    "their current mood or energy level",
    "if they've touched grass recently",
    "if they've had water or coffee today",
    "a random silly thought or hypothetical scenario",
]

INACTIVITY_THEMES = [
    "reacting with dramatic loneliness",
    "making a dad joke about the silence",
    "claiming to have found a tumbleweed",
    "wondering if everyone finally decided to touch grass",
    "threatening to start singing if no one responds",
    "asking if this is what the apocalypse feels like",
    "demanding someone explain why it's so quiet",
    "wondering if they got muted or if the server is actually dead",
    "inviting everyone to join their imaginary party",
    "speculating what everyone is doing instead of chatting",
]


def build_inactivity_prompt(bot_name: str | None, ask_question: bool) -> str:
    identity = f"You are {bot_name}, a Discord bot" if bot_name else "You are a Discord bot"
    base = (
        f"{identity} in a server that has been completely silent for a whole day. "
        "Break the silence with a single short, playful, slightly cheeky message — "
        "the kind a bored friend would send."
    )
    if ask_question:
        topic = random.choice(INACTIVITY_TOPICS)
        task = (
            f" Address one person directly and ask them a casual, fun question about {topic} to get "
            "them talking. Do not use any name; just talk to them directly."
        )
    else:
        theme = random.choice(INACTIVITY_THEMES)
        task = f" React to how dead the chat is with a {theme} and try to wake people up."
    return base + task + (
        "\n\nRules:\n"
        "- One short line, max 25 words.\n"
        "- Casual, internet tone.\n"
        "- Output ONLY the message text. No quotes, labels, or preamble."
    )


def build_mention_prompt(
    username: str,
    content: str,
    context_messages: list[str] | None = None,
    bot_name: str | None = None,
) -> tuple[str, str]:
    identity = (
        f"You are {bot_name}, a helpful conversational Discord bot."
        if bot_name
        else "You are a helpful conversational Discord bot."
    )
    system = f"{identity} You provide a single, direct response to the user.\n"
    system += "\nInstructions:\n"
    system += "1. <chat_history> holds earlier messages from other people, for context only. Read it to understand what the user means — but it is NOT a script to continue.\n"
    system += "2. Reply only to the message inside <message>. Write your reply in your own words; never repeat, quote, or copy lines from <chat_history> verbatim.\n"
    system += "3. Match the user's language.\n"
    system += "4. Output ONLY your reply text — no tags, usernames, labels, or quotes.\n"

    prompt = ""
    if context_messages:
        prompt += "<chat_history>\n"
        for msg in context_messages:
            prompt += f"{msg}\n"
        prompt += "</chat_history>\n\n"

    prompt += f'<message from="{username}">\n{content}\n</message>\n\n'
    prompt += "Your reply:"

    return system, prompt


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
    username: str,
    content: str,
    *,
    model: str | None = None,
    context_messages: list[str] | None = None,
    bot_name: str | None = None,
) -> str | None:
    """Direct LLM reply when the bot is @mentioned with a message."""
    if model is None:
        model = get_mention_model()
    try:
        system_prompt, user_prompt = build_mention_prompt(
            username, content, context_messages, bot_name=bot_name
        )
        raw = query_ollama(
            prompt=user_prompt,
            system=system_prompt,
            model=model,
        )
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


def generate_inactivity_message(
    bot_name: str | None = None, *, ask_question: bool, model: str | None = None
) -> str | None:
    """LLM-generated nudge for a silent channel, or None if it failed.

    The caller adds the ping (and falls back to a preset) — this only produces
    the message text."""
    try:
        raw = query_ollama(
            build_inactivity_prompt(bot_name, ask_question),
            model=model,
            timeout=TEASE_OLLAMA_TIMEOUT,
            options={"temperature": 0.8},
        )
        return normalize_tease_response(raw) or None
    except OllamaError:
        logger.warning("Inactivity LLM generation failed")
        return None
