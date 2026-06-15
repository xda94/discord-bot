import logging
import os

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


def normalize_tease_response(text: str) -> str:
    cleaned = text.strip().strip("\"'").strip()
    if "\n" in cleaned:
        cleaned = cleaned.split("\n", 1)[0].strip()

    if len(cleaned) > TEASE_LLM_MAX_CHARS:
        trimmed = cleaned[:TEASE_LLM_MAX_CHARS].rsplit(" ", 1)[0]
        cleaned = trimmed or cleaned[:TEASE_LLM_MAX_CHARS]
    return cleaned


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
