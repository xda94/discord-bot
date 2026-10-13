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
from features.assistant_profiles import AssistantProfilesFeature
from features.birthdays import BirthdaysFeature
from features.bingo import BingoFeature
from features.humor import HumorFeature
from features.llm_mention import ContextReactionFeature, LLMMentionFeature
from features.natural_commands import NaturalCommandsFeature
from features.help_feature import HelpFeature
from features.onboarding import OnboardingFeature
from features.quote_museum import QuoteMuseumFeature
from features.flights import FlightTrackerFeature
from features.inactivity import InactivityFeature
from features.jokes import JokesFeature
from features.keywords import KeywordsFeature
from features.llm_feedback import LLMFeedbackFeature
from features.reminders import RemindersFeature
from notifications import NotificationsFeature
from features.response_gate import ResponseGate
from features.wishlist import WishlistFeature
from features.sponsors import SponsorsFeature
from features.stats import StatsFeature
from features.social_games import SocialGamesFeature
from features.teases import TeasesFeature
from features.user_memory import (
    UserMemoryFeature,
    is_automatic_memory_enabled,
    is_memory_disabled,
)

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
assistant_profiles = AssistantProfilesFeature(client, tree)
stats = StatsFeature(client, tree)
azi_se_spala = AziSeSpalaFeature(client, tree)
llm_feedback = LLMFeedbackFeature(client, tree, BOT_ID)
social_games = SocialGamesFeature(client, tree)
quote_museum = QuoteMuseumFeature(client, tree, social_games)
bingo = BingoFeature(client, tree)
humor = HumorFeature(client, tree)
MEMORY_DISABLED = is_memory_disabled()
AUTOMATIC_MEMORY_ENABLED = not MEMORY_DISABLED and is_automatic_memory_enabled()
user_memory = (
    None
    if MEMORY_DISABLED
    else UserMemoryFeature(client, tree, automatic_enabled=AUTOMATIC_MEMORY_ENABLED)
)
llm_mention = LLMMentionFeature(
    client,
    tree,
    bot_id=BOT_ID,
    feedback=llm_feedback,
    memory=user_memory,
)
natural_commands = NaturalCommandsFeature(
    client,
    bot_id=BOT_ID,
    wishlist=wishlist,
    flights=flights,
    reminders=reminders,
)
llm_mention.natural_commands = natural_commands
context_reactions = ContextReactionFeature(llm_mention)
help_feature = HelpFeature(
    client,
    tree,
    automatic_memory_enabled=AUTOMATIC_MEMORY_ENABLED,
    memory_disabled=MEMORY_DISABLED,
)
onboarding = OnboardingFeature(client,tree,memory_disabled=MEMORY_DISABLED)
logger.info(
    "LLM memory mode=%s",
    "disabled" if MEMORY_DISABLED else ("automatic" if AUTOMATIC_MEMORY_ENABLED else "manual"),
)

MESSAGE_HANDLERS = (
    (inactivity, user_memory, natural_commands, llm_mention, keywords, context_reactions, teases)
    if AUTOMATIC_MEMORY_ENABLED
    else (inactivity, natural_commands, llm_mention, keywords, context_reactions, teases)
)

BACKGROUND_FEATURES = (
    sponsors,
    inactivity,
    reminders,
    notifications,
    natural_commands,
    jokes,
    birthdays,
    wishlist,
    flights,
    llm_mention,
    social_games,
    quote_museum,
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


@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    await llm_feedback.handle_raw_reaction_add(payload)


try:
    client.run(TOKEN)
except KeyboardInterrupt:
    logger.info("Received keyboard interrupt; shutting down.")
except Exception:
    logger.exception("Bot crashed with an unhandled exception")
    raise
finally:
    logger.info("Bot process exited.")
