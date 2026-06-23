from __future__ import annotations

import logging
import os
import random
import time

import discord
from discord import app_commands
from discord.ext import tasks

import db

logger = logging.getLogger("discord_bot")

SPONSOR_TIERS = {
    "standard": {"name": "Sponsor Standard", "price": "6 lei / an", "chance": 0.01},
    "entuziast": {"name": "Sponsor Entuziast", "price": "8 lei / an", "chance": 0.03},
    "premium": {"name": "Sponsor Premium", "price": "10 lei / an", "chance": 0.05},
    "ultra": {"name": "Sponsor Ultra Pro Max", "price": "20 lei / an", "chance": 0.08},
}

SPONSOR_TIER_CHOICES = [
    app_commands.Choice(name=t["name"], value=k) for k, t in SPONSOR_TIERS.items()
]

ONE_YEAR_SECONDS = 365 * 24 * 3600
ONE_DAY_SECONDS = 24 * 3600


class _SponsorModal(discord.ui.Modal, title="Set Sponsor"):
    password = discord.ui.TextInput(
        label="Password", placeholder="Enter the password", max_length=100
    )
    custom_message = discord.ui.TextInput(
        label="Custom message (Ultra Pro Max only)",
        placeholder="Leave empty if not Ultra Pro Max",
        required=False,
        max_length=200,
    )

    def __init__(self, feature: "SponsorsFeature", sponsor_name: str | None, tier: str):
        super().__init__()
        self._feature = feature
        self._sponsor_name = sponsor_name
        self._tier = tier

    async def on_submit(self, interaction: discord.Interaction):
        if self.password.value != self._feature.password:
            await interaction.response.send_message("Wrong password.", ephemeral=True)
            return

        logger.info(
            f"Command /sponsor_set called by {interaction.user} "
            f"with name={self._sponsor_name}, tier={self._tier}"
        )
        custom = self.custom_message.value if self._tier == "ultra" else None
        self._feature.apply(self._sponsor_name, self._tier, custom)

        if self._sponsor_name:
            tier_info = SPONSOR_TIERS.get(self._tier, SPONSOR_TIERS["standard"])
            await interaction.response.send_message(
                f"Sponsor set to **{self._sponsor_name}** with plan **{tier_info['name']}**.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message("Sponsor cleared.", ephemeral=True)


class SponsorsFeature:
    """Owns sponsor state and the /sponsor_* commands."""

    def __init__(self, client: discord.Client, tree: app_commands.CommandTree):
        self.client = client
        self.tree = tree
        self.password = os.getenv("SPONSOR_PASSWORD")

        self.sponsor: str | None = db.get_setting("sponsor") or None
        set_at = db.get_setting("sponsor_set_at")
        self.sponsor_set_at: float | None = float(set_at) if set_at else None
        tier = db.get_setting("sponsor_tier")
        self.sponsor_tier: str = tier if tier in SPONSOR_TIERS else "standard"
        self.sponsor_custom_message: str | None = db.get_setting("sponsor_custom_message") or None
        # Persisted so we don't re-announce the "expires in 1 day" warning on
        # every bot restart that lands inside the final-day window.
        self.sponsor_warned: bool = db.get_setting("sponsor_warned") == "1"

        self._register_commands()

    def maybe_get_sponsor_suffix(self) -> str | None:
        """Return a sponsor suffix (e.g. ' (Sponsored by Bob)') for the current
        tier's roll, or None if no sponsor or the roll failed."""
        if not self.sponsor:
            return None
        tier = SPONSOR_TIERS.get(self.sponsor_tier, SPONSOR_TIERS["standard"])
        if random.random() >= tier["chance"]:
            return None
        if self.sponsor_tier == "ultra" and self.sponsor_custom_message:
            return f" ({self.sponsor_custom_message})"
        return f" (Sponsored by {self.sponsor})"

    def apply(self, sponsor_name: str | None, tier: str, custom_message: str | None) -> None:
        self.sponsor = sponsor_name
        if sponsor_name:
            self.sponsor_set_at = time.time()
            self.sponsor_warned = False
            self.sponsor_tier = tier
            self.sponsor_custom_message = custom_message or None
            db.set_setting("sponsor", sponsor_name)
            db.set_setting("sponsor_set_at", str(self.sponsor_set_at))
            db.set_setting("sponsor_tier", tier)
            db.set_setting("sponsor_custom_message", custom_message or "")
            db.set_setting("sponsor_warned", "0")
        else:
            self.sponsor_set_at = None
            self.sponsor_warned = False
            self.sponsor_tier = "standard"
            self.sponsor_custom_message = None
            db.set_setting("sponsor", "")
            db.set_setting("sponsor_set_at", "")
            db.set_setting("sponsor_tier", "")
            db.set_setting("sponsor_custom_message", "")
            db.set_setting("sponsor_warned", "0")

    async def start_tasks(self) -> None:
        if not self._check_expiry.is_running():
            self._check_expiry.start()

    def _register_commands(self) -> None:
        feature = self

        @self.tree.command(name="sponsor_set", description="Set or clear the sponsor tag")
        @app_commands.describe(user="Select the sponsor user (omit to clear)", plan="Sponsorship plan")
        @app_commands.choices(plan=SPONSOR_TIER_CHOICES)
        async def sponsor_set(
            interaction: discord.Interaction,
            user: discord.Member | None = None,
            plan: app_commands.Choice[str] | None = None,
        ):
            sponsor_name = user.display_name if user else None
            tier = plan.value if plan else "standard"
            await interaction.response.send_modal(_SponsorModal(feature, sponsor_name, tier))

        @self.tree.command(name="sponsor_plans", description="Show available sponsorship plans")
        async def sponsor_plans(interaction: discord.Interaction):
            logger.info(f"Command /sponsor_plans called by {interaction.user}")
            text = (
                "**Available Sponsorship Plans:**\n\n"
                "**Sponsor Standard** — 6 lei / an — 1% sansa sa adauge la un raspuns `(Sponsored by @User)`\n"
                "**Sponsor Entuziast** — 8 lei / an — 3% sansa sa adauge la un raspuns `(Sponsored by @User)`\n"
                "**Sponsor Premium** — 10 lei / an — 5% sansa sa adauge la un raspuns `(Sponsored by @User)`\n"
                "**Sponsor Ultra Pro Max** — 20 lei / an — 8% sansa sa adauge la un raspuns un mesaj pe care il vrei tu"
            )
            await interaction.response.send_message(text)

        @self.tree.command(name="sponsor_who", description="Show the current sponsor and time until expiry")
        async def sponsor_who(interaction: discord.Interaction):
            logger.info(f"Command /sponsor_who called by {interaction.user}")
            if not feature.sponsor or not feature.sponsor_set_at:
                await interaction.response.send_message(
                    "There is no active sponsor right now.", ephemeral=True
                )
                return

            elapsed = time.time() - feature.sponsor_set_at
            remaining = ONE_YEAR_SECONDS - elapsed
            if remaining <= 0:
                await interaction.response.send_message(
                    "The sponsorship has expired.", ephemeral=True
                )
                return

            days = int(remaining // 86400)
            hours = int((remaining % 86400) // 3600)
            minutes = int((remaining % 3600) // 60)
            tier_info = SPONSOR_TIERS.get(feature.sponsor_tier, SPONSOR_TIERS["standard"])
            await interaction.response.send_message(
                f"**Current Sponsor:** {feature.sponsor}\n"
                f"**Plan:** {tier_info['name']}\n"
                f"**Expires in:** {days}d {hours}h {minutes}m"
            )

    @tasks.loop(hours=1)
    async def _check_expiry(self):
        try:
            if not self.sponsor or not self.sponsor_set_at:
                return

            elapsed = time.time() - self.sponsor_set_at
            one_day_before = ONE_YEAR_SECONDS - ONE_DAY_SECONDS

            if elapsed >= one_day_before and not self.sponsor_warned:
                # Persist *before* sending so a crash mid-broadcast still
                # prevents a duplicate warning on the next bot start.
                self.sponsor_warned = True
                db.set_setting("sponsor_warned", "1")
                for guild in self.client.guilds:
                    channel = guild.system_channel or next(
                        (ch for ch in guild.text_channels if ch.permissions_for(guild.me).send_messages),
                        None,
                    )
                    if channel:
                        await channel.send(
                            f"@everyone Sponsorship for **{self.sponsor}** is going to expire in one day. "
                            f"Who would like to be the next sponsor?"
                        )
                logger.info(f"Sponsor expiry warning sent for '{self.sponsor}'")

            if elapsed >= ONE_YEAR_SECONDS:
                logger.info(f"Sponsor '{self.sponsor}' has expired")
                self.apply(None, "standard", None)
        except Exception:
            logger.exception("Error in check_sponsor_expiry task")
