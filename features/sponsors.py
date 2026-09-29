from __future__ import annotations

import hmac
import logging
import os
import random
import time
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import tasks

import db
from analytics import record_for
from sponsor_tiers import (
    DEFAULT_SPONSOR_TIERS,
    DuplicateTierNameError,
    TierCatalogError,
    TierValidationError,
    UnknownTierError,
    format_annual_price,
    format_chance,
)

logger = logging.getLogger("discord_bot")

# Compatibility mapping for code/tests that imported the old module constant.
# Runtime consumers use the persisted catalog through `_get_catalog` instead.
SPONSOR_TIERS = {
    tier_id: {
        **deepcopy(tier),
        "price": format_annual_price(tier["price_per_year"]),
    }
    for tier_id, tier in DEFAULT_SPONSOR_TIERS.items()
}
SPONSOR_TIER_CHOICES = [
    app_commands.Choice(name=tier["name"], value=tier_id)
    for tier_id, tier in SPONSOR_TIERS.items()
]

ONE_YEAR_SECONDS = 365 * 24 * 3600
ONE_DAY_SECONDS = 24 * 3600
DISCORD_MESSAGE_LIMIT = 2000


def _catalog_items(catalog: dict[str, dict[str, Any]]):
    return catalog.items()


def _tier_price(tier: dict[str, Any]) -> str:
    # The compatibility `price` key is intentionally honored for callers that
    # still customize the old module-level mapping in tests/extensions.
    return tier.get("price") or format_annual_price(tier["price_per_year"])


def _tier_chance(tier: dict[str, Any]) -> str:
    return format_chance(tier["chance"])


def build_sponsor_plans_message(
    catalog: Optional[dict[str, dict[str, Any]]] = None,
) -> str:
    """Build the public plan list from an explicit or compatibility catalog."""
    catalog = SPONSOR_TIERS if catalog is None else catalog
    lines = ["**Available Sponsorship Plans:**", ""]
    for tier_id, tier in _catalog_items(catalog):
        benefit = (
            "sansa sa adauge la un raspuns un mesaj pe care il vrei tu"
            if tier_id == "ultra"
            else "sansa sa adauge la un raspuns `(Sponsored by @User)`"
        )
        lines.append(
            f"**{tier['name']}** — {_tier_price(tier)} — {_tier_chance(tier)} {benefit}"
        )
    return "\n".join(lines)


def _split_sponsor_plans_message(
    catalog: dict[str, dict[str, Any]],
    limit: int = DISCORD_MESSAGE_LIMIT,
) -> list[str]:
    """Split the plan list at tier boundaries, not arbitrary text positions."""
    header = ["**Available Sponsorship Plans:**", ""]
    blocks = []
    for tier_id, tier in _catalog_items(catalog):
        benefit = (
            "sansa sa adauge la un raspuns un mesaj pe care il vrei tu"
            if tier_id == "ultra"
            else "sansa sa adauge la un raspuns `(Sponsored by @User)`"
        )
        blocks.append(
            f"**{tier['name']}** — {_tier_price(tier)} — {_tier_chance(tier)} {benefit}"
        )

    messages: list[str] = []
    current = list(header)
    for block in blocks:
        candidate = "\n".join(current + [block])
        if len(candidate) > limit and len(current) > len(header):
            messages.append("\n".join(current))
            current = [block]
        else:
            current.append(block)
    if current:
        messages.append("\n".join(current))
    return messages


def _password_error(feature: "SponsorsFeature", submitted: str) -> Optional[str]:
    if not feature.password:
        return "Sponsor password is not configured."
    if not hmac.compare_digest(str(submitted), feature.password):
        return "Wrong password."
    return None


def _percent_to_probability(value: str) -> Decimal:
    try:
        percent = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise TierValidationError("chance must be a number from 0 to 100 percent") from None
    if not percent.is_finite() or percent < 0 or percent > 100:
        raise TierValidationError("chance must be a number from 0 to 100 percent")
    return percent / Decimal("100")


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
        password_error = _password_error(self._feature, self.password.value)
        if password_error:
            await record_for("failure", "sponsor-modal-submit", interaction)
            await interaction.response.send_message(password_error, ephemeral=True)
            return

        catalog = self._feature._get_catalog()
        if self._sponsor_name and self._tier not in catalog:
            await record_for("failure", "sponsor-modal-submit", interaction)
            await interaction.response.send_message(
                "That sponsorship plan no longer exists. Run /sponsor-set again.",
                ephemeral=True,
            )
            return

        logger.info(
            "Command /sponsor-set called by %s with name=%s, tier=%s",
            interaction.user,
            self._sponsor_name,
            self._tier,
        )
        custom = self.custom_message.value if self._tier == "ultra" else None
        self._feature.apply(self._sponsor_name, self._tier, custom)

        if self._sponsor_name:
            tier_info = catalog.get(self._tier, catalog["standard"])
            await interaction.response.send_message(
                f"Sponsor set to **{self._sponsor_name}** with plan **{tier_info['name']}**.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message("Sponsor cleared.", ephemeral=True)
        await record_for("control", "sponsor-modal-submit", interaction)


class _SponsorTierModal(discord.ui.Modal, title="Edit Sponsor Tier"):
    password = discord.ui.TextInput(
        label="Password", placeholder="Enter the sponsor password", max_length=100
    )
    tier_name = discord.ui.TextInput(
        label="Tier name", placeholder="Displayed plan name", max_length=80
    )
    annual_price = discord.ui.TextInput(
        label="Annual price in lei", placeholder="e.g. 12.50", max_length=30
    )
    chance_percent = discord.ui.TextInput(
        label="Chance (%)", placeholder="0 to 100, e.g. 12.5", max_length=30
    )

    def __init__(self, feature: "SponsorsFeature", tier_id: str | None, tier: Optional[dict[str, Any]]):
        super().__init__()
        self._feature = feature
        self._tier_id = tier_id
        if tier is not None:
            self.tier_name.default = str(tier["name"])
            self.annual_price.default = str(tier["price_per_year"])
            self.chance_percent.default = format_chance(tier["chance"]).removesuffix("%")

    async def on_submit(self, interaction: discord.Interaction):
        activity = "sponsor-tiers-modal-submit"
        password_error = _password_error(self._feature, self.password.value)
        if password_error:
            await record_for("failure", activity, interaction)
            await interaction.response.send_message(password_error, ephemeral=True)
            return

        try:
            # Re-check the selected ID against the latest catalog because the
            # modal may have remained open while another process edited it.
            catalog = self._feature._get_catalog()
            if self._tier_id is not None and self._tier_id not in catalog:
                raise UnknownTierError("That tier no longer exists; reopen the editor.")
            saved = db.save_sponsor_tier(
                self._tier_id,
                self.tier_name.value,
                self.annual_price.value,
                _percent_to_probability(self.chance_percent.value),
            )
        except DuplicateTierNameError as exc:
            await record_for("failure", activity, interaction)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except (TierValidationError, UnknownTierError, TierCatalogError) as exc:
            await record_for("failure", activity, interaction)
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except Exception:
            logger.exception("Could not save sponsor tier")
            await record_for("failure", activity, interaction)
            await interaction.response.send_message(
                "Could not save the sponsor tier. No changes were acknowledged.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Sponsor tier **{saved['name']}** saved.", ephemeral=True
        )
        await record_for("control", activity, interaction)


class SponsorsFeature:
    """Owns sponsor state and the /sponsor-* commands."""

    def __init__(self, client: discord.Client, tree: app_commands.CommandTree):
        self.client = client
        self.tree = tree
        self.password = os.getenv("SPONSOR_PASSWORD")

        self.sponsor: str | None = db.get_setting("sponsor") or None
        set_at = db.get_setting("sponsor_set_at")
        self.sponsor_set_at: float | None = float(set_at) if set_at else None
        # Retain an unknown persisted ID so a temporarily missing custom tier
        # is not silently rewritten; consumers apply the standard fallback.
        self.sponsor_tier: str = db.get_setting("sponsor_tier") or "standard"
        self.sponsor_custom_message: str | None = db.get_setting("sponsor_custom_message") or None
        # Persisted so we don't re-announce the "expires in 1 day" warning on
        # every bot restart that lands inside the final-day window.
        self.sponsor_warned: bool = db.get_setting("sponsor_warned") == "1"

        self._register_commands()

    def _get_catalog(self) -> dict[str, dict[str, Any]]:
        try:
            return db.get_sponsor_tiers()
        except Exception:
            logger.exception("Could not load sponsor tiers; using built-in defaults")
            return deepcopy(DEFAULT_SPONSOR_TIERS)

    async def _tier_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        query = (current or "").casefold()
        results = []
        for tier_id, tier in self._get_catalog().items():
            if query and query not in tier_id.casefold() and query not in tier["name"].casefold():
                continue
            label = f"{tier['name']} ({tier_id})"
            results.append(app_commands.Choice(name=label[:100], value=tier_id))
            if len(results) == 25:
                break
        return results

    def maybe_get_sponsor_suffix(self) -> str | None:
        """Return a probabilistic suffix for the current effective tier."""
        if not self.sponsor:
            return None
        catalog = self._get_catalog()
        tier = catalog.get(self.sponsor_tier, catalog["standard"])
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

        @self.tree.command(name="sponsor-set", description="Set or clear the sponsor tag")
        @app_commands.describe(user="Select the sponsor user (omit to clear)", plan="Sponsorship plan")
        async def sponsor_set(
            interaction: discord.Interaction,
            user: Optional[discord.Member] = None,
            plan: Optional[str] = None,
        ):
            sponsor_name = user.display_name if user else None
            tier = plan or "standard"
            if sponsor_name and tier not in feature._get_catalog():
                await record_for("failure", "sponsor-modal-submit", interaction)
                await interaction.response.send_message(
                    "Unknown sponsorship plan. Choose a plan from autocomplete.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_modal(_SponsorModal(feature, sponsor_name, tier))

        @sponsor_set.autocomplete("plan")
        async def sponsor_set_plan_autocomplete(interaction: discord.Interaction, current: str):
            return await feature._tier_autocomplete(interaction, current)

        @self.tree.command(
            name="sponsor-set-tiers", description="Create or edit a sponsorship tier"
        )
        @app_commands.describe(plan="Existing tier to edit; omit to create a tier")
        async def sponsor_set_tiers(
            interaction: discord.Interaction,
            plan: Optional[str] = None,
        ):
            catalog = feature._get_catalog()
            if plan is not None and plan not in catalog:
                await record_for("failure", "sponsor-tiers-modal-submit", interaction)
                await interaction.response.send_message(
                    "Unknown sponsorship tier. Choose a tier from autocomplete.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_modal(
                _SponsorTierModal(feature, plan, catalog.get(plan) if plan else None)
            )

        @sponsor_set_tiers.autocomplete("plan")
        async def sponsor_set_tiers_plan_autocomplete(interaction: discord.Interaction, current: str):
            return await feature._tier_autocomplete(interaction, current)

        @self.tree.command(name="sponsor-plans", description="Show available sponsorship plans")
        async def sponsor_plans(interaction: discord.Interaction):
            logger.info("Command /sponsor-plans called by %s", interaction.user)
            messages = _split_sponsor_plans_message(feature._get_catalog())
            await interaction.response.send_message(messages[0])
            for message in messages[1:]:
                await interaction.followup.send(message)

        @self.tree.command(name="sponsor-who", description="Show the current sponsor and time until expiry")
        async def sponsor_who(interaction: discord.Interaction):
            logger.info("Command /sponsor-who called by %s", interaction.user)
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
            catalog = feature._get_catalog()
            tier_info = catalog.get(feature.sponsor_tier, catalog["standard"])
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
                        try:
                            await channel.send(
                                f"@everyone Sponsorship for **{self.sponsor}** is going to expire in one day. "
                                "Who would like to be the next sponsor?"
                            )
                            await record_for("scheduled", "sponsor-expiry-warning", channel)
                        except Exception:
                            await record_for("failure", "sponsor-expiry-warning", channel)
                            logger.exception("Could not send sponsor expiry warning in guild %s", guild.id)
                logger.info("Sponsor expiry warning sent for '%s'", self.sponsor)

            if elapsed >= ONE_YEAR_SECONDS:
                logger.info("Sponsor '%s' has expired", self.sponsor)
                self.apply(None, "standard", None)
        except Exception:
            logger.exception("Error in check_sponsor_expiry task")
