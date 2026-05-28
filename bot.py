import os
import sys

import discord
from discord import app_commands
from dotenv import load_dotenv

# IMPORTANT: configure logging BEFORE importing any feature module. Some
# feature modules (e.g. scraping) emit `logger.warning` at import time to
# announce missing optional dependencies, and those warnings are lost if the
# logger has no handlers attached yet.
from logger import setup_logger

logger = setup_logger("discord_bot", "bot.log")

import db
from features.help_feature import HelpFeature
from features.inactivity import InactivityFeature
from features.jokes import JokesFeature
from features.keywords import KeywordsFeature
from features.reminders import RemindersFeature
from features.response_gate import ResponseGate
from features.scraping import ScrapingFeature
from features.sponsors import SponsorsFeature
from features.stats import StatsFeature
from features.teases import TeasesFeature

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Fail loud and early when required env vars are missing. discord.py raises an
# opaque LoginFailure deep in its stack when handed an empty token; this
# message is far easier to action.
if not TOKEN:
    logger.critical(
        "DISCORD_TOKEN is not set. Add it to your .env file or environment "
        "and restart. Refusing to start."
    )
    sys.exit(1)

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

db.init_db()

# Shared cooldown gate (keyword auto-responses and teases throttle off the same
# clock so the bot never spams the channel).
gate = ResponseGate()

# Order matters only for dependencies: KeywordsFeature reads the sponsor suffix
# from SponsorsFeature, so sponsors must be built first.
sponsors = SponsorsFeature(client, tree)
keywords = KeywordsFeature(client, tree, gate, sponsors)
teases = TeasesFeature(client, tree)
inactivity = InactivityFeature(client, tree)
reminders = RemindersFeature(client, tree)
jokes = JokesFeature(client, tree)
scraping = ScrapingFeature(client, tree)
stats = StatsFeature(client, tree)
help_feature = HelpFeature(client, tree)

# Features that observe every message. Ordering reflects the original on_message
# flow: track activity first, then try a keyword match, then a tease.
MESSAGE_HANDLERS = (inactivity, keywords, teases)

# Features that own background tasks needing to be kicked off in on_ready.
BACKGROUND_FEATURES = (sponsors, inactivity, reminders, jokes, scraping)


@client.event
async def on_ready():
    try:
        await tree.sync()
        for feature in BACKGROUND_FEATURES:
            await feature.start_tasks()
        logger.info(f"Bot is ready! Logged in as {client.user} (ID: {client.user.id})")
    except Exception:
        logger.exception("Error during on_ready startup sequence")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    for handler in MESSAGE_HANDLERS:
        # A handler returns True if it produced a "real" response and wants the
        # dispatch chain to stop (currently only KeywordsFeature does this).
        if await handler.handle_message(message):
            return


# discord.py's `client.run()` installs its own SIGINT handler and closes the
# client cleanly on Ctrl+C / `pm2 stop`, but it does so silently. The
# try/finally surface gives us a clear "shutting down" + "exited" pair in
# `bot.log` so we can confirm a clean exit at a glance (and catch the case
# where the process gets SIGKILL'd, which leaves the "exited" line missing).
try:
    client.run(TOKEN)
except KeyboardInterrupt:
    logger.info("Received keyboard interrupt; shutting down.")
except Exception:
    logger.exception("Bot crashed with an unhandled exception")
    raise
finally:
    logger.info("Bot process exited.")
