import logging

import discord
from discord import app_commands

logger = logging.getLogger("discord_bot")

HELP_TEXT = (
    "**Available Commands**\n\n"
    "**/keyword_add** `<keyword>` `<response>`\n"
    "Add a keyword-response pair. When someone types a message containing the keyword, "
    "the bot replies with the response. Multiple responses can be added to the same keyword — "
    "the bot picks one at random.\n\n"
    "**/remind** `<when>` `<who>` `<what>`\n"
    "Set a reminder. The bot will ping the specified user after the given time. "
    "Time format: `30m` (minutes), `2h` (hours), `1d` (days).\n\n"
    "**/topkeywords** `[user]`\n"
    "Show the most triggered keywords in this server. "
    "Optionally pass a user to see their personal keyword stats.\n\n"
    "**/mood** `<mood>`\n"
    "Set the bot's mood. Changes the style of random tease messages. Default mood is `bad`. "
    "Moods: `bad`, `good`, `computer`, `gen-z`, `dad`, `anime`, or `random`. "
    "Resets the tease counter for the day.\n\n"
    "**/joke_add** `<text>`\n"
    "Add a joke/text to the daily joke list. "
    "Once all jokes are sent, the cycle resets.\n\n"
    "**/joke_activation** `<time>`\n"
    "Activate the daily joke in this channel at the specified time (e.g. `14:00`).\n\n"
    "**/sponsor_plans**\n"
    "Show the available sponsorship plans and pricing.\n\n"
    "**/sponsor_who**\n"
    "Show who the current sponsor is, their plan, and how much time remains until expiry.\n\n"
    "**/scrape-item** `<url>`\n"
    "Track a product URL — the bot will check its price and stock every 12 hours and DM you "
    "on changes.\n\n"
    "**/scrape-item-delete** `<url>`\n"
    "Stop tracking a URL and remove its price history.\n\n"
    "**/scrape-show** `[currency]`\n"
    "List every URL you currently track with its latest price and stock status. "
    "Pick a currency (RON, DKK, EUR, USD, GBP) to convert every row into it; "
    "defaults to RON.\n\n"
    "**/scrape-graph** `<url>` `[currency]`\n"
    "Generate a price-evolution chart for a single tracked URL. Optional "
    "`currency` (RON, DKK, EUR, USD, GBP) converts the Y-axis; defaults to RON.\n\n"
    "**/scrape-graph-all** `[currency]`\n"
    "Generate a combined price-evolution chart across **all** your tracked items, with prices "
    "normalized to the chosen currency (RON, DKK, EUR, USD, GBP) so different-currency items "
    "can be compared on one axis. Defaults to RON.\n\n"
    "**/stats**\n"
    "Show hardware stats: CPU, RAM, disk, temperature, network, uptime, and bot memory usage.\n\n"
    "**/help**\n"
    "Show this message."
)


class HelpFeature:
    """The /help command."""

    def __init__(self, client: discord.Client, tree: app_commands.CommandTree):
        self.client = client
        self.tree = tree
        self._register_commands()

    def _register_commands(self) -> None:
        @self.tree.command(
            name="help", description="Show all available commands and how to use them"
        )
        async def help_cmd(interaction: discord.Interaction):
            logger.info(f"Command /help called by {interaction.user}")
            await interaction.response.send_message(HELP_TEXT, ephemeral=True)
