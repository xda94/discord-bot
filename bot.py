import os
import re
import time
import random
from datetime import date
import requests
import discord
import logging
from logging.handlers import RotatingFileHandler
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from db import (
    init_db, add_response, get_random_response, get_all_responses,
    add_reminder, get_due_reminders, delete_reminder,
    log_keyword_usage, get_top_keywords, get_top_keywords_by_user
)

# --- Enhanced Logging Setup ---
logger = logging.getLogger("discord_bot")
logger.setLevel(logging.INFO)

formatter = logging.Formatter('%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s')

# Writes to bot.log
file_handler = RotatingFileHandler('bot.log', maxBytes=5*1024*1024, backupCount=2)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Prints to console
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# Link DB logger
db_logger = logging.getLogger("database")
db_logger.setLevel(logging.INFO)
db_logger.addHandler(file_handler)
db_logger.addHandler(console_handler)
# ------------------------------

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')

intents = discord.Intents.default()
intents.message_content = True

last_response_time = 0
COOLDOWN = 10
TEASE_BASE_CHANCE = 0.10
teases_today = 0
tease_reset_date = None
current_mood = "bad"
TEASE_MOODS = {
    "bad": [
        "interesting take, {user}",
        "nobody asked, {user}",
        "cool story, {user}",
        "sure thing, {user}",
        "ok buddy",
        "are you done?",
        "tell me more... actually don't",
        "that's crazy, anyway",
        "I'm going to pretend I didn't read that",
        "{user} really typed that and hit send",
        "taci dracu...",
        "iar s-a trezit asta",
    ],
    "good": [
        "great point, {user}!",
        "I appreciate you, {user}",
        "that's a really good take, {user}",
        "couldn't have said it better myself",
        "you're on fire today, {user}",
        "{user} spitting facts as usual",
        "W take, {user}",
        "this is why {user} is the goat",
        "based, {user}",
        "finally someone with good taste",
    ],
    "computer": [
        "01001000 01101001",
        "SYNTAX ERROR: {user} not found in database",
        "sudo rm -rf {user}",
        "segfault at 0x00000000 in {user}.exe",
        "ERROR 418: I'm a teapot",
        "[WARN] {user}.dll has stopped responding",
        "ping {user} ... Request timed out",
        "git blame {user}",
        "404: good take not found",
        "while(true) {{ {user} }}",
        "// TODO: understand what {user} just said",
        "{user} has mass = NaN kg",
    ],
    "gen-z": [
        "no cap {user} just ate",
        "that's lowkey sus, {user}",
        "skill issue, {user}",
        "rent free in {user}'s head",
        "{user} understood the assignment",
        "it's giving {user}",
        "slay i guess, {user}",
        "{user} really said that with their whole chest",
        "that ain't it chief",
        "big yikes from {user}",
        "let him cook",
        "are we cooked, chat?",
    ],
    "dad": [
        "Hi {user}, I'm bot",
        "Back in my day, we didn't say stuff like that, {user}",
        "Don't make me turn this server around",
        "Ask your mother, {user}",
        "That's what she said... wait, who said that",
        "{user}, pull my finger",
        "You call that a message? Now MY messages, those were messages",
        "Pe vremea mea mergeam la scoala pe jos prin zapada",
    ],
    "anime": [
        "N-nani?! {user} said WHAT?!",
        "Omae wa mou shindeiru, {user}",
        "{user} just activated my trap card",
        "This isn't even my final form, {user}",
        "{user}'s power level is over 9000!!",
        "You fool, {user}! You fell for it!",
        "{user} has the power of friendship and anime on their side",
        "A wild {user} appeared!",
        "uwu",
    ],
}

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# Initialize DB
init_db()

@client.event
async def on_ready():
    try:
        await tree.sync()
        if not check_reminders.is_running():
            check_reminders.start()
        logger.info(f"Bot is ready! Logged in as {client.user} (ID: {client.user.id})")
    except Exception:
        logger.exception("Error during on_ready startup sequence")

@client.event
async def on_message(message):
    global last_response_time

    if message.author.bot:
        return

    now = time.time()
    if now - last_response_time < COOLDOWN:
        return

    content = message.content.lower()
    
    try:
        all_keywords = get_all_responses().keys()
        for keyword in all_keywords:
            if re.search(r'\b' + re.escape(keyword) + r'\b', content):
                response = get_random_response(keyword)
                if response:
                    await message.reply(response, mention_author=False)
                    last_response_time = now
                    if message.guild:
                        log_keyword_usage(keyword, message.author.id, message.guild.id)
                    logger.info(f"Triggered response for '{keyword}' in #{message.channel}")
                    return
    except Exception:
        logger.exception("Error processing message for keywords")

    # Random tease — decaying chance so it doesn't spam on busy days
    global teases_today, tease_reset_date
    today = date.today()
    if tease_reset_date != today:
        teases_today = 0
        tease_reset_date = today
    chance = TEASE_BASE_CHANCE / (1 + teases_today)
    if random.random() < chance:
        tease = random.choice(TEASE_MOODS[current_mood]).format(user=message.author.display_name)
        await message.reply(tease, mention_author=False)
        teases_today += 1
        logger.info(f"Tease #{teases_today} triggered on {message.author} in #{message.channel}")

@tree.command(name="add", description="Add a new keyword and response")
@app_commands.describe(keyword="The keyword", response="The response")
async def add(interaction: discord.Interaction, keyword: str, response: str):
    logger.info(f"Command /add called by {interaction.user} for '{keyword}'")
    add_response(keyword, response)
    await interaction.response.send_message(f"Added keyword: **{keyword}**")

def parse_time(time_str):
    minutes_per_unit = {"m": 1, "h": 60, "d": 1440}
    match = re.match(r"(\d+)([mhd])", time_str.lower())
    if not match:
        return None
    amount, unit = match.groups()
    return int(amount) * minutes_per_unit[unit] * 60

@tasks.loop(seconds=10)
async def check_reminders():
    try:
        due = get_due_reminders()
        for rem_id, user_id, channel_id, message in due:
            try:
                # Attempt to send the message
                channel = client.get_channel(channel_id)
                if channel:
                    await channel.send(f"🔔 <@{user_id}>, here is your reminder: **{message}**")
                    logger.info(f"Delivered reminder {rem_id} to user {user_id}")
                else:
                    logger.warning(f"Reminder {rem_id}: Channel {channel_id} inaccessible")
            except Exception as e:
                # If sending fails (e.g., missing perms), log it but DO NOT crash the loop
                logger.error(f"Failed to send reminder {rem_id}: {e}")
            finally:
                # CRITICAL: Always delete the reminder so we don't spam errors infinitely
                delete_reminder(rem_id)
                
    except Exception:
        logger.exception("Critical error inside check_reminders loop")

@tree.command(name="remind", description="Set a reminder")
@app_commands.describe(when="Time (e.g. 30m, 1h)", who="User to remind", what="The message")
async def remind(interaction: discord.Interaction, when: str, who: discord.Member, what: str):
    logger.info(f"Command /remind called by {interaction.user} targeting {who.display_name}")
    
    seconds = parse_time(when)
    if seconds is None:
        await interaction.response.send_message("Invalid time format! Use 1m, 1h, or 1d.", ephemeral=True)
        return

    remind_at = time.time() + seconds
    add_reminder(who.id, interaction.channel_id, remind_at, what)
    
    await interaction.response.send_message(f"Got it! I'll remind {who.display_name} about '{what}' in {when}.")

@tree.command(name="topkeywords", description="Show the most used keywords")
@app_commands.describe(user="Optional: see a specific user's top keywords")
async def topkeywords(interaction: discord.Interaction, user: discord.Member = None):
    logger.info(f"Command /topkeywords called by {interaction.user}" + (f" for {user.display_name}" if user else ""))
    if not interaction.guild:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    guild_id = interaction.guild.id

    if user:
        rows = get_top_keywords_by_user(guild_id, user.id)
        title = f"Top keywords for {user.display_name}"
    else:
        rows = get_top_keywords(guild_id)
        title = "Top keywords in this server"

    if not rows:
        await interaction.response.send_message("No keyword usage data yet.", ephemeral=True)
        return

    lines = [f"**{title}**"]
    for i, (keyword, count) in enumerate(rows, 1):
        display = keyword if keyword.startswith("<@") else f"`{keyword}`"
        lines.append(f"{i}. {display} — {count} use{'s' if count != 1 else ''}")

    await interaction.response.send_message("\n".join(lines))

@tree.command(name="mood", description="Set the bot's mood")
@app_commands.describe(mood="The mood to set")
@app_commands.choices(mood=[
    app_commands.Choice(name="bad", value="bad"),
    app_commands.Choice(name="good", value="good"),
    app_commands.Choice(name="computer", value="computer"),
    app_commands.Choice(name="gen-z", value="gen-z"),
    app_commands.Choice(name="dad", value="dad"),
    app_commands.Choice(name="anime", value="anime"),
    app_commands.Choice(name="random", value="random"),
])
async def mood(interaction: discord.Interaction, mood: app_commands.Choice[str]):
    global current_mood, teases_today
    chosen = mood.value
    if chosen == "random":
        chosen = random.choice(list(TEASE_MOODS.keys()))
    logger.info(f"Command /mood called by {interaction.user} — setting mood to {chosen}")
    current_mood = chosen
    teases_today = 0
    await interaction.response.send_message(f"Mood set to **{mood.value}**.")

@tree.command(name="help", description="Show all available commands and how to use them")
async def help(interaction: discord.Interaction):
    logger.info(f"Command /help called by {interaction.user}")
    text = (
        "**Available Commands**\n\n"
        "**/add** `<keyword>` `<response>`\n"
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
        "Set the bot's mood. Changes the style of random tease messages. "
        "Moods: `bad`, `good`, `computer`, "
        "`gen-z`, `dad`, `anime`, or `random`. "
        "Resets the tease counter for the day.\n\n"
        "**/help**\n"
        "Show this message."
    )
    await interaction.response.send_message(text, ephemeral=True)

client.run(TOKEN)