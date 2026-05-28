import logging

import discord
from discord import app_commands

logger = logging.getLogger("discord_bot")

# Discord rejects any single slash-command response > 2000 chars (the
# interaction silently times out, which looks like a broken command from
# the user's side). 1900 leaves a small safety margin for invisible
# formatting overhead.
DISCORD_MESSAGE_LIMIT = 1900


def _chunk_text(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Split `text` into chunks <= `limit` chars, breaking on paragraph
    boundaries (`\\n\\n`) so a command's description never gets cut in half.

    Falls back to emitting an oversized chunk verbatim if a single
    paragraph already exceeds `limit` — Discord will then reject just that
    chunk, which is louder than silently truncating it."""
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) > limit and current:
            chunks.append(current)
            current = para
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks

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
    "By default each row is shown in its own native currency (as quoted by the merchant, "
    "or guessed from the URL's TLD). Pass `currency` (RON, DKK, EUR, USD, GBP) to convert "
    "every row into that single currency instead.\n\n"
    "**/scrape-graph** `<url>` `[currency]`\n"
    "Generate a price-evolution chart for a single tracked URL. Defaults to the item's "
    "own currency (no conversion). Optional `currency` (RON, DKK, EUR, USD, GBP) converts "
    "the Y-axis into that unit instead.\n\n"
    "**/scrape-graph-all** `[currency]`\n"
    "Generate a combined price-evolution chart across **all** your tracked items. Defaults "
    "to the majority currency in your list (so the largest number of items appear without "
    "conversion). Optional `currency` (RON, DKK, EUR, USD, GBP) overrides the default and "
    "normalizes every series to that single currency.\n\n"
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
            chunks = _chunk_text(HELP_TEXT)
            # First chunk satisfies Discord's initial interaction-response
            # contract; the rest go through follow-ups on the same token.
            await interaction.response.send_message(chunks[0], ephemeral=True)
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk, ephemeral=True)
