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
    "Add a keyword-response pair **for this server only** (not shared across servers). "
    "When someone types a message containing the keyword, the bot replies with the response. "
    "Multiple responses can be added to the same keyword — the bot picks one at random.\n\n"
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
    "Add a joke/text to the daily joke list. The pool is global \u2014 shared across every server. "
    "Each server cycles through it independently, and once a server has seen every joke its pool resets.\n\n"
    "**/joke_activation** `<time>`\n"
    "Activate the daily joke in **this channel and server** at the specified time (e.g. `14:00`). "
    "Each server is configured independently \u2014 running this in two servers schedules two independent daily jokes.\n\n"
    "**/joke_deactivation**\n"
    "Stop the daily joke in this server. The server's sent-joke history is preserved, so re-activating later "
    "doesn't replay jokes it already received.\n\n"
    "**/joke_status**\n"
    "Show this server's daily-joke configuration (channel, time, last sent date), or that it's not activated. "
    "Reply is only visible to you.\n\n"
    "**/sponsor_plans**\n"
    "Show the available sponsorship plans and pricing.\n\n"
    "**/sponsor_who**\n"
    "Show who the current sponsor is, their plan, and how much time remains until expiry.\n\n"
    "**/wishlist-item** `<url>`\n"
    "Track a product URL — the bot will check its price and stock every 12 hours and DM you "
    "on changes. You'll also be DMed with buy-signals: 🟢 when the price hits a new all-time "
    "low (\"buy window\"), and 🔴 when it climbs above the historical median (\"maybe wait\"). "
    "Buy-signals need at least ~3 days of history before they start firing, and only "
    "kick in once the price has actually moved by at least ~1 % over the tracked window "
    "(perfectly-flat prices stay quiet — no spurious 'all-time low' DMs).\n\n"
    "**/wishlist-item-delete** `<url>`\n"
    "Stop tracking a URL and remove its price history.\n\n"
    "**/wishlist-show** `[currency]`\n"
    "List every URL you currently track with its latest price and stock status. "
    "By default each row is shown in its own native currency (as quoted by the merchant, "
    "or guessed from the URL's TLD). Pass `currency` (RON, DKK, EUR, USD, GBP) to convert "
    "every row into that single currency instead.\n\n"
    "**/wishlist-graph** `<url>` `[currency]`\n"
    "Generate a price-evolution chart for a single tracked URL. Defaults to the item's "
    "own currency (no conversion). Optional `currency` (RON, DKK, EUR, USD, GBP) converts "
    "the Y-axis into that unit instead. History retention: up to 6 months.\n\n"
    "**/wishlist-graph-all** `[currency]`\n"
    "Generate a combined price-evolution chart across **all** your tracked items. Defaults "
    "to the majority currency in your list (so the largest number of items appear without "
    "conversion). Optional `currency` (RON, DKK, EUR, USD, GBP) overrides the default and "
    "normalizes every series to that single currency. History retention: up to 6 months.\n\n"
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
