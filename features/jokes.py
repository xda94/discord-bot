import logging
from datetime import date, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import tasks

import db

logger = logging.getLogger("discord_bot")


class JokesFeature:
    """Daily joke loop plus per-guild activation, deactivation, and status
    commands.

    Each guild that runs `/joke_activation` gets its own row in
    `guild_joke_config` with its own channel + send time + last-sent
    date. The 30-second `_check` loop iterates every configured guild
    and fires that guild's next unsent joke when its scheduled window
    arrives. Sent-joke history is tracked per guild in
    `guild_joke_sent` so the same joke can run in different guilds
    independently while still preserving the no-repeats-until-pool-
    exhausts contract within each guild.

    Legacy migration: on first start after upgrading from the old
    single-guild design, `_migrate_legacy_config` reads the deprecated
    `joke_channel_id` / `joke_send_time` / `joke_last_sent_date`
    settings, looks up the owning guild via Discord, writes a row in
    `guild_joke_config`, and clears the legacy keys. Idempotent: once
    the legacy keys are gone, it's a no-op."""

    LEGACY_SETTING_KEYS = (
        "joke_channel_id",
        "joke_send_time",
        "joke_last_sent_date",
    )

    def __init__(self, client: discord.Client, tree: app_commands.CommandTree):
        self.client = client
        self.tree = tree
        self._migrated = False
        self._register_commands()

    async def start_tasks(self) -> None:
        # Migration must run AFTER `on_ready` because it needs the
        # client to be able to call `fetch_channel`. Running it inside
        # `start_tasks` (called from `on_ready`) is the right hook.
        await self._migrate_legacy_config()
        if not self._check.is_running():
            self._check.start()

    async def _migrate_legacy_config(self) -> None:
        """One-shot import of the old single-guild config into the new
        per-guild schema. Safe to call repeatedly — bails immediately
        once the legacy keys are gone."""
        if self._migrated:
            return

        legacy_channel = db.get_setting("joke_channel_id")
        if not legacy_channel:
            # Nothing to migrate, or a previous run already cleared the
            # legacy keys. Mark done so we stop re-checking.
            self._migrated = True
            return

        try:
            channel = await self.client.fetch_channel(int(legacy_channel))
        except (discord.NotFound, discord.Forbidden) as exc:
            logger.warning(
                f"Legacy joke channel {legacy_channel} unreachable ({type(exc).__name__}); "
                f"clearing the legacy config. Re-run /joke_activation in the "
                f"target guild to opt back in."
            )
            for key in self.LEGACY_SETTING_KEYS:
                db.set_setting(key, "")
            self._migrated = True
            return
        except Exception:
            logger.exception(
                f"Failed to fetch legacy joke channel {legacy_channel}; will "
                f"retry on next bot start."
            )
            # Don't mark migrated — give it another chance after a
            # transient failure (e.g. network blip during fetch).
            return

        guild_id = getattr(channel, "guild", None) and channel.guild.id
        if guild_id is None:
            # DM channel or some other non-guild target — can't migrate.
            logger.warning(
                f"Legacy joke channel {legacy_channel} has no guild "
                f"(probably a DM); clearing the legacy config."
            )
            for key in self.LEGACY_SETTING_KEYS:
                db.set_setting(key, "")
            self._migrated = True
            return

        send_time = db.get_setting("joke_send_time") or "12:00"
        last_sent = db.get_setting("joke_last_sent_date") or None

        db.set_guild_joke_config(guild_id, channel.id, send_time)
        if last_sent:
            db.set_guild_joke_last_sent(guild_id, last_sent)

        # Clear the legacy keys so this branch becomes a no-op next time.
        # Using empty string (falsy) rather than DELETE because the
        # settings helper has no delete; empty value reads as None via
        # the truthiness checks above.
        for key in self.LEGACY_SETTING_KEYS:
            db.set_setting(key, "")

        logger.info(
            f"Migrated legacy joke config to guild {guild_id} "
            f"(channel={channel.id}, time={send_time}, last_sent={last_sent})"
        )
        self._migrated = True

    def _register_commands(self) -> None:
        feature = self

        @self.tree.command(name="joke_add", description="Add a joke/text to the daily joke list")
        @app_commands.describe(text="The joke or text to add")
        async def joke_add(interaction: discord.Interaction, text: str):
            logger.info(f"Command /joke_add called by {interaction.user}")
            db.add_joke(text)
            await interaction.response.send_message("Joke added!")

        @self.tree.command(
            name="joke_activation",
            description="Activate the daily joke in this channel at a specific time",
        )
        @app_commands.describe(time="The time to send the daily joke (e.g. 14:00)")
        async def joke_activation(interaction: discord.Interaction, time: str):
            logger.info(
                f"Command /joke_activation called by {interaction.user} "
                f"in guild {interaction.guild_id} with time {time}"
            )
            if interaction.guild_id is None:
                await interaction.response.send_message(
                    "This command must be run inside a server, not in DMs.",
                    ephemeral=True,
                )
                return
            try:
                datetime.strptime(time, "%H:%M")
            except ValueError:
                await interaction.response.send_message(
                    "Invalid time format! Use HH:MM (e.g. `14:00`).", ephemeral=True
                )
                return

            db.set_guild_joke_config(
                interaction.guild_id, interaction.channel_id, time
            )
            await interaction.response.send_message(
                f"Daily joke activated in this channel at **{time}** every day."
            )

        @self.tree.command(
            name="joke_deactivation",
            description="Stop the daily joke in this server",
        )
        async def joke_deactivation(interaction: discord.Interaction):
            logger.info(
                f"Command /joke_deactivation called by {interaction.user} "
                f"in guild {interaction.guild_id}"
            )
            if interaction.guild_id is None:
                await interaction.response.send_message(
                    "This command must be run inside a server, not in DMs.",
                    ephemeral=True,
                )
                return

            removed = db.clear_guild_joke_config(interaction.guild_id)
            if removed:
                await interaction.response.send_message(
                    "Daily joke deactivated for this server. "
                    "Run `/joke_activation` again to re-enable."
                )
            else:
                await interaction.response.send_message(
                    "There was no daily joke scheduled for this server.",
                    ephemeral=True,
                )

        @self.tree.command(
            name="joke_status",
            description="Show the daily joke configuration for this server",
        )
        async def joke_status(interaction: discord.Interaction):
            logger.info(
                f"Command /joke_status called by {interaction.user} "
                f"in guild {interaction.guild_id}"
            )
            if interaction.guild_id is None:
                await interaction.response.send_message(
                    "This command must be run inside a server, not in DMs.",
                    ephemeral=True,
                )
                return

            cfg = db.get_guild_joke_config(interaction.guild_id)
            if cfg is None:
                await interaction.response.send_message(
                    "Daily joke is **not activated** for this server. "
                    "Use `/joke_activation` to enable it.",
                    ephemeral=True,
                )
                return

            channel_mention = f"<#{cfg['channel_id']}>"
            last_sent = cfg["last_sent_date"] or "never"
            await interaction.response.send_message(
                f"📅 Daily joke is **active** in {channel_mention} at "
                f"**{cfg['send_time']}** every day.\n"
                f"Last sent: `{last_sent}`",
                ephemeral=True,
            )

    @tasks.loop(seconds=30)
    async def _check(self):
        try:
            now = datetime.now()
            today = now.date()
            today_iso = today.isoformat()

            for cfg in db.get_all_guild_joke_configs():
                guild_id = cfg["guild_id"]
                try:
                    target = datetime.strptime(cfg["send_time"], "%H:%M").replace(
                        year=now.year, month=now.month, day=now.day
                    )
                except ValueError:
                    logger.warning(
                        f"Guild {guild_id} has invalid send_time "
                        f"{cfg['send_time']!r}; skipping."
                    )
                    continue

                if cfg["last_sent_date"] == today_iso:
                    continue
                if not (target <= now <= target + timedelta(minutes=2)):
                    continue

                await self._send_joke_for_guild(guild_id, cfg["channel_id"], today_iso)
        except Exception:
            logger.exception("Error in daily_joke_check task")

    async def _send_joke_for_guild(
        self, guild_id: int, channel_id: int, today_iso: str
    ) -> None:
        """Pick one unsent joke for this guild, post it, and persist
        sent state. Isolated per-guild so a failure in one guild's send
        doesn't break the iteration in `_check` (caller wraps in
        try/except for the whole loop, but per-guild safety is cheap)."""
        result = db.get_unsent_joke_for_guild(guild_id)
        if result is None:
            # Pool exhausted for this guild — recycle and retry.
            db.reset_guild_joke_sent(guild_id)
            result = db.get_unsent_joke_for_guild(guild_id)
            if result is None:
                logger.info(
                    f"No jokes in database. Skipping daily joke for guild {guild_id}."
                )
                return

        joke_id, text = result
        channel = self.client.get_channel(channel_id)
        if not channel:
            logger.warning(
                f"Joke channel {channel_id} (guild {guild_id}) not accessible — "
                f"check that the bot is still in the guild and has read access."
            )
            return

        try:
            await channel.send(f"**Joke of the day:**\n{text}", suppress_embeds=True)
        except discord.Forbidden:
            logger.warning(
                f"Missing permissions to post in channel {channel_id} "
                f"(guild {guild_id}); skipping joke."
            )
            return

        db.mark_guild_joke_sent(guild_id, joke_id)
        db.set_guild_joke_last_sent(guild_id, today_iso)
        logger.info(f"Daily joke {joke_id} sent in guild {guild_id}")
