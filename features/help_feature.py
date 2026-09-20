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
    "**/keyword-add** `<keyword>` `<response>`\n"
    "Add a keyword-response pair **for this server only** (not shared across servers). "
    "When someone types a message containing the keyword, the bot replies with the response. "
    "Multiple responses can be added to the same keyword — the bot picks one at random.\n\n"
    "**/remind** `<when>` `<who>` `<what>`\n"
    "Set a reminder. The bot will ping the specified user after the given time. "
    "Time format: `30m` (minutes), `2h` (hours), `1d` (days).\n\n"
    "**/top-keywords** `[user]`\n"
    "Show the most triggered keywords in this server. "
    "Optionally pass a user to see their personal keyword stats.\n\n"
    "**/mood** `<mood>`\n"
    "Set the bot's mood. Changes the style of random tease messages — each tease is sent "
    "through llama.cpp to rewrite it in that mood while keeping the same gist. "
    "Moods: `bad`, `good`, `computer`, `gen-z`, `dad`, `anime`, `shy`, `lenghel`, or `random`. "
    "Resets the tease counter for the day.\n\n"
    "**/joke-add** `<text>`\n"
    "Add a joke/text to the daily joke list. The pool is global \u2014 shared across every server. "
    "Each server cycles through it independently, and once a server has seen every joke its pool resets.\n\n"
    "**/joke-activation** `<time>`\n"
    "Activate the daily joke in **this channel and server** at the specified time (e.g. `14:00`). "
    "Each server is configured independently \u2014 running this in two servers schedules two independent daily jokes.\n\n"
    "**/joke-deactivation**\n"
    "Stop the daily joke in this server. The server's sent-joke history is preserved, so re-activating later "
    "doesn't replay jokes it already received.\n\n"
    "**/joke-status**\n"
    "Show this server's daily-joke configuration (channel, time, last sent date), or that it's not activated. "
    "Reply is only visible to you.\n\n"
    "**/sponsor-plans**\n"
    "Show the available sponsorship plans and pricing.\n\n"
    "**/sponsor-who**\n"
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
    "**/wishlist-target-price** <url> <price> <currency>\n"
    "Notify once when a tracked item reaches or drops below your chosen target. "
    "Targets support RON, DKK, EUR, USD, and GBP.\n\n"
    "**/wishlist-target-clear** <url>\n"
    "Remove the target-price alert for one tracked item.\n\n"
    "**/wishlist-restock-only** <url> <enabled>\n"
    "When enabled, keep collecting price history but only DM when an out-of-stock "
    "item becomes available again.\n\n"
    "**/wishlist-refresh** `[url]`\n"
    "Refresh only the supplied tracked URL, or omit it to refresh your entire wishlist. "
    "Shows each source, current status, and target progress. Each item has its own "
    "five-minute cooldown; cooling items are skipped during an all-item refresh.\n\n"
    "**/wishlist-show** `[currency]`\n"
    "List every URL you currently track with its latest price and stock status. "
    "By default each row is shown in its own native currency (as quoted by the merchant, "
    "or guessed from the URL's TLD). Pass `currency` (RON, DKK, EUR, USD, GBP) to convert "
    "every row into that single currency instead.\n\n"
    "**/wishlist-graph** `<url>` `[currency]` `[days]`\n"
    "Generate a price-evolution chart for a single tracked URL. Defaults to the item's "
    "own currency (no conversion). Optional `currency` (RON, DKK, EUR, USD, GBP) converts "
    "the Y-axis into that unit instead. Set days to any whole number from 1–180 (default 180). "
    "Use 7/30/90-day buttons or Custom days on the graph.\n\n"
    "**/wishlist-graph-all** `[currency]` `[days]` `[percentage]`\n"
    "Generate a combined price-evolution chart across **all** your tracked items. Defaults "
    "to the majority currency in your list (so the largest number of items appear without "
    "conversion). Optional `currency` (RON, DKK, EUR, USD, GBP) overrides the default and "
    "converts prices to that currency. Set days to 1–180 (default 180). "
    "Use percentage:true or the comparison button for relative changes, starting at 0% "
    "for each product's first observation in the period. Controls are requester-only "
    "and expire after ten idle minutes. Dates are in UTC.\n\n"
    "**/flight-tracker-add** `<origin>` `<destination>` `<start-date>` `<end-date>` "
    "`[adults]` `[currency]`\n"
    "Track a fixed-date round trip per user using 3-letter IATA codes and YYYY-MM-DD dates. "
    "The first time you use it, a private login modal asks for your single SerpApi API Key. "
    "It is validated and saved per Discord user. "
    "The bot checks every 5 hours by default and DMs when it finds the first price or a "
    "lower price.\n\n"
    "**/flight-tracker-show**\n"
    "List your saved routes, periods, tracker IDs, and latest prices.\n\n"
    "**/flight-tracker-delete** `<tracker-id>`\n"
    "Delete one of your own flight trackers and its price history.\n\n"
    "**/flight-tracker-login**\n"
    "Validate and replace your private SerpApi API Key.\n\n"
    "**/flight-tracker-logout**\n"
    "Remove your saved SerpApi key; existing trackers stay saved but paused.\n\n"
    "**/stats**\n"
    "Show portable host stats: platform, CPU/cores, RAM, current filesystem, network, "
    "uptime, and bot memory. Temperature/load display N/A when unavailable.\n\n"
    "**/azi-se-spala** `[data]`\n"
    "Check whether today is a day you can do laundry, per the Romanian Orthodox calendar "
    "(data from azisespala.ro). On holy days tradition says you don't wash — the bot tells "
    "you which it is and names the day. Optionally pass a date (`ZZ.LL` or `ZZ.LL.AAAA`, "
    "e.g. `25.12`) to check another day.\n\n"
    "**/llm-set** `<model>`\n"
    "Select one of the llama.cpp model aliases configured in `.env` "
    "(`LLAMA_CPP_ALLOWED_MODELS`).\n\n"
    "**/llm-inactivity** `<activate|deactivate>`\n"
    "Activate or deactivate automatic LLM inactivity nudges for this server. "
    "Requires the **Manage Server** permission.\n\n"
    "**/llm-memory** `<activate|deactivate|status>`\n"
    "Manage persistent user memory in the current channel. Memory starts disabled, "
    "and changing it requires **Manage Server**. Activation posts a public privacy "
    "notice. Deactivation stops capture/use but retains saved memory.\n\n"
    "**/llm-memory-purge** `<confirmation>`\n"
    "Server managers can enter `PURGE` to permanently remove all saved memory "
    "for this server. Channel settings and individual opt-outs remain unchanged.\n\n"
    "**/memory-show**\n"
    "Privately show the saved memory synthesis for you in this server or DM, including "
    "facts, impressions, likes, dislikes, and topics. Raw chat is deleted after synthesis.\n\n"
    "**/memory-forget**\n"
    "Erase all saved memory and pending observations here without opting out.\n\n"
    "**/memory-opt-out** / **/memory-opt-in**\n"
    "Stop and erase persistent memory for yourself, or enable it again. DM memory "
    "is separate from server memory and requires explicit opt-in.\n\n"
    "**@bot** (mention)\n"
    "Ping the bot: it replies directly in the thread — no model name, no question "
    "echo, no thinking message. Empty ping gets a short \"what do you need?\" style "
    "reply; ping with text gets an LLM answer to that text. Model: `MENTION_LLAMA_CPP_MODEL` "
    "in `.env`. In memory-enabled channels, the same requester's synthesized facts, "
    "impressions, preferences, and topics supplement live chat history. One request per user at a time with "
    "a cooldown after completion. "
    "Set `BOT_ID` in `.env` to your bot's user ID.\n\n"
    "The person who asked may manually add 👍 or 👎 to an LLM mention reply. "
    "Contextual emoji reactions are added to human messages, never as seeded feedback "
    "on bot replies. Feedback from anyone else is ignored.\n\n"
    "**/llm-feedback-summary**\n"
    "Server managers can compare rated LLM reply configurations for this server. "
    "The report needs at least 10 ratings per model/prompt combination before "
    "it marks a comparison as ready.\n\n"
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
