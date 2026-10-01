"""Validation and effective defaults for global per-user assistant profiles."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from wishlist.currency import CurrencyConverter


LANGUAGES = ("auto", "en", "ro")
TONES = ("default", "friendly", "formal", "playful")
CURRENCIES = CurrencyConverter.SUPPORTED_DISPLAY_CURRENCIES
NOTIFICATION_STYLES = ("standard", "compact")
LLM_BEHAVIORS = ("balanced", "concise", "detailed")


@dataclass(frozen=True)
class AssistantProfile:
    language: str = "auto"
    tone: str = "default"
    currency: str | None = None
    timezone: str = "UTC"
    notification_style: str = "standard"
    llm_behavior: str = "balanced"

    def as_dict(self) -> dict:
        return asdict(self)


DEFAULT_PROFILE = AssistantProfile()


def validate_timezone(value: str) -> str:
    timezone = value.strip()
    if not timezone:
        raise ValueError("Timezone cannot be empty.")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(
            "Timezone must be a valid IANA name such as `Europe/Bucharest` or `UTC`."
        ) from None
    return timezone


def validate_profile_updates(**updates) -> dict:
    """Validate a partial update without filling or changing omitted fields."""
    allowed = {
        "language", "tone", "currency", "timezone",
        "notification_style", "llm_behavior",
    }
    unknown = set(updates) - allowed
    if unknown:
        raise ValueError(f"Unknown profile field: {sorted(unknown)[0]}")

    clean = {}
    for field, value in updates.items():
        if field == "currency":
            value = value.upper() if isinstance(value, str) and value else None
            if value is not None and value not in CURRENCIES:
                raise ValueError(f"Currency must be one of: {', '.join(CURRENCIES)}.")
        elif field == "timezone":
            value = validate_timezone(value)
        else:
            choices = {
                "language": LANGUAGES,
                "tone": TONES,
                "notification_style": NOTIFICATION_STYLES,
                "llm_behavior": LLM_BEHAVIORS,
            }[field]
            if value not in choices:
                raise ValueError(f"{field.replace('_', ' ').title()} must be one of: {', '.join(choices)}.")
        clean[field] = value
    return clean


def effective_profile(stored: Mapping | AssistantProfile | None) -> AssistantProfile:
    """Return an immutable complete snapshot; a missing row uses defaults."""
    if stored is None:
        return DEFAULT_PROFILE
    if isinstance(stored, AssistantProfile):
        return stored
    values = DEFAULT_PROFILE.as_dict()
    values.update({key: stored[key] for key in values if key in stored})
    return AssistantProfile(**validate_profile_updates(**values))


def profile_prompt_preferences(profile: AssistantProfile) -> str:
    """Render bounded language/style guidance for mention prompt builders."""
    if profile == DEFAULT_PROFILE:
        return ""
    language = {
        "auto": "infer the reply language from the current request",
        "en": "use English unless the requester explicitly asks for another language",
        "ro": "use Romanian unless the requester explicitly asks for another language",
    }[profile.language]
    tone = {
        "default": "use a natural tone appropriate to the request",
        "friendly": "use a warm, friendly tone",
        "formal": "use a professional, formal tone",
        "playful": "use a light, playful tone when appropriate",
    }[profile.tone]
    detail = {
        "balanced": "use a balanced amount of detail",
        "concise": "be concise",
        "detailed": "give a detailed answer within the response limit",
    }[profile.llm_behavior]
    return f"Preferences unless explicitly overridden: {language}; {tone}; {detail}."
