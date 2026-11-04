from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

load_dotenv()

import discord

from logger import setup_logger

logger = setup_logger("discord_bot", "bot.log")

import db
from analytics import AnalyticsCommandTree, record_message_mention, refresh_command_catalog
from features.azi_se_spala import AziSeSpalaFeature
from features.birthdays import BirthdaysFeature
from features.humor import HumorFeature
from features.llm_mention import ContextReactionFeature, LLMMentionFeature
from features.help_feature import HelpFeature
from features.onboarding import OnboardingFeature
from features.flights import FlightTrackerFeature
from features.inactivity import InactivityFeature
from features.jokes import JokesFeature
from features.keywords import KeywordsFeature
from features.reminders import RemindersFeature
from notifications import NotificationsFeature
from features.response_gate import ResponseGate
from features.wishlist import WishlistFeature
from features.sponsors import SponsorsFeature
from features.stats import StatsFeature
from features.teases import TeasesFeature

from llm.client import LlamaCppError, get_default_model

try:
    get_default_model()
except LlamaCppError as exc:
    logger.critical("%s Refusing to start.", exc)
    sys.exit(1)

_raw_bot_id = os.getenv("BOT_ID", "").strip()
if not _raw_bot_id:
    logger.critical(
        "BOT_ID is not set. Add your bot's Discord user ID to .env and restart."
    )
    sys.exit(1)
try:
    BOT_ID = int(_raw_bot_id)
except ValueError:
    logger.critical("BOT_ID must be a numeric Discord user ID.")
    sys.exit(1)

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    logger.critical(
        "DISCORD_TOKEN is not set. Add it to your .env file or environment "
        "and restart. Refusing to start."
    )
    sys.exit(1)

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
tree = AnalyticsCommandTree(client)

db.init_db()

gate = ResponseGate()

sponsors = SponsorsFeature(client, tree)
keywords = KeywordsFeature(client, tree, gate, sponsors)
teases = TeasesFeature(client, tree)
inactivity = InactivityFeature(client, tree)
reminders = RemindersFeature(client, tree)
notifications = NotificationsFeature(client)
jokes = JokesFeature(client, tree)
birthdays = BirthdaysFeature(client, tree)
wishlist = WishlistFeature(client, tree)
flights = FlightTrackerFeature(client, tree)
stats = StatsFeature(client, tree)
azi_se_spala = AziSeSpalaFeature(client, tree)
humor = HumorFeature(client, tree)
llm_mention = LLMMentionFeature(
    client,
    tree,
    bot_id=BOT_ID,
)
context_reactions = ContextReactionFeature(llm_mention)
help_feature = HelpFeature(client, tree)
onboarding = OnboardingFeature(client, tree)

MESSAGE_HANDLERS = (inactivity, llm_mention, keywords, context_reactions, teases)

BACKGROUND_FEATURES = (
    sponsors,
    inactivity,
    reminders,
    notifications,
    jokes,
    birthdays,
    wishlist,
    flights,
    llm_mention,
)


@client.event
async def on_ready():
    try:
        await tree.sync()
    except Exception:
        logger.exception("Could not synchronize slash commands")
    try:
        await refresh_command_catalog(tree)
    except Exception:
        logger.exception("Could not refresh slash command catalog")
    for feature in BACKGROUND_FEATURES:
        try:
            await feature.start_tasks()
        except Exception:
            logger.exception("Could not start background feature %s",type(feature).__name__)
    logger.info(f"Bot is ready! Logged in as {client.user} (ID: {client.user.id})")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    await record_message_mention(message, BOT_ID)
    for handler in MESSAGE_HANDLERS:
        if await handler.handle_message(message):
            return


try:
    client.run(TOKEN)
except KeyboardInterrupt:
    logger.info("Received keyboard interrupt; shutting down.")
except Exception:
    logger.exception("Bot crashed with an unhandled exception")
    raise
finally:
    logger.info("Bot process exited.")
