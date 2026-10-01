"""Private slash commands for global per-user assistant preferences."""

from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands

import db
from assistant_profiles import (
    CURRENCIES,
    LANGUAGES,
    LLM_BEHAVIORS,
    NOTIFICATION_STYLES,
    TONES,
    effective_profile,
    validate_profile_updates,
)


def _choices(values):
    return [app_commands.Choice(name=value, value=value) for value in values]


def format_profile(profile) -> str:
    currency = profile.currency or "feature default"
    return (
        "**Your assistant profile**\n"
        f"Language: `{profile.language}`\n"
        f"Tone: `{profile.tone}`\n"
        f"Currency: `{currency}`\n"
        f"Timezone: `{profile.timezone}`\n"
        f"Notifications: `{profile.notification_style}`\n"
        f"LLM behavior: `{profile.llm_behavior}`"
    )


class AssistantProfilesFeature:
    def __init__(self, client: discord.Client, tree: app_commands.CommandTree):
        self.client = client
        self.tree = tree
        self._register_commands()

    def _register_commands(self) -> None:
        @self.tree.command(name="assistant-profile", description="Show your private assistant preferences")
        async def assistant_profile(interaction: discord.Interaction):
            profile = effective_profile(db.get_assistant_profile(interaction.user.id))
            await interaction.response.send_message(format_profile(profile), ephemeral=True)

        @self.tree.command(name="assistant-profile-set", description="Update your private assistant preferences")
        @app_commands.describe(
            language="Preferred reply language",
            tone="Preferred assistant tone",
            currency="Default currency, or unset to preserve each feature's default",
            timezone="IANA timezone, e.g. Europe/Bucharest",
            notification_style="Standard or compact wishlist/flight alerts",
            llm_behavior="Preferred LLM answer detail",
        )
        @app_commands.choices(
            language=_choices(LANGUAGES),
            tone=_choices(TONES),
            currency=_choices((*CURRENCIES, "unset")),
            notification_style=_choices(NOTIFICATION_STYLES),
            llm_behavior=_choices(LLM_BEHAVIORS),
        )
        @app_commands.rename(
            notification_style="notification-style",
            llm_behavior="llm-behavior",
        )
        async def assistant_profile_set(
            interaction: discord.Interaction,
            language: Optional[app_commands.Choice[str]] = None,
            tone: Optional[app_commands.Choice[str]] = None,
            currency: Optional[app_commands.Choice[str]] = None,
            timezone: Optional[str] = None,
            notification_style: Optional[app_commands.Choice[str]] = None,
            llm_behavior: Optional[app_commands.Choice[str]] = None,
        ):
            updates = {}
            for name, choice in (
                ("language", language), ("tone", tone),
                ("notification_style", notification_style),
                ("llm_behavior", llm_behavior),
            ):
                if choice is not None:
                    updates[name] = choice.value
            if currency is not None:
                updates["currency"] = None if currency.value == "unset" else currency.value
            if timezone is not None:
                updates["timezone"] = timezone
            if not updates:
                await interaction.response.send_message(
                    "Choose at least one preference to update.", ephemeral=True
                )
                return
            try:
                updates = validate_profile_updates(**updates)
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            stored = db.set_assistant_profile(interaction.user.id, **updates)
            if stored is None:
                await interaction.response.send_message(
                    "I couldn't save your assistant profile. Please try again.", ephemeral=True
                )
                return
            await interaction.response.send_message(
                "Saved.\n" + format_profile(effective_profile(stored)), ephemeral=True
            )

        @self.tree.command(name="assistant-profile-reset", description="Reset your assistant preferences to defaults")
        async def assistant_profile_reset(interaction: discord.Interaction):
            reset = db.reset_assistant_profile(interaction.user.id)
            if reset is None:
                await interaction.response.send_message(
                    "I couldn't reset your assistant profile. Please try again.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                "Your assistant profile was reset.\n" + format_profile(effective_profile(None)),
                ephemeral=True,
            )
