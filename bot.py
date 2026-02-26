import os
import re
import time
import requests
import discord
import logging
from logging.handlers import RotatingFileHandler
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from db import (
    init_db, get_random_response, get_all_responses,
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
HOST = os.getenv('HOST')
PORT = os.getenv('PORT')

API_URL = f"http://{HOST}:{PORT}"

intents = discord.Intents.default()
intents.message_content = True

last_response_time = 0
COOLDOWN = 10

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# Initialize DB
init_db()

def add_keyword_response(keyword, response):
    payload = {"keyword": keyword, "response": response}
    url = f"{API_URL}/add"
    try:
        r = requests.post(url, json=payload)
        r.raise_for_status()
        logger.info(f"API Success: Added keyword '{keyword}' via {url}")
        return r.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"API Request Failed at {url}: {e}")
        return {"error": str(e)}

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
            if keyword in content:
                response = get_random_response(keyword)
                if response:
                    await message.reply(response, mention_author=False)
                    last_response_time = now
                    if message.guild:
                        log_keyword_usage(keyword, message.author.id, message.guild.id)
                    logger.info(f"Triggered response for '{keyword}' in #{message.channel}")
                    break
    except Exception:
        logger.exception("Error processing message for keywords")

@tree.command(name="add", description="Add a new keyword and response")
@app_commands.describe(keyword="The keyword", response="The response")
async def add(interaction: discord.Interaction, keyword: str, response: str):
    logger.info(f"Command /add called by {interaction.user} for '{keyword}'")
    result = add_keyword_response(keyword, response)
    
    if "status" in result and result["status"] == "ok":
         await interaction.response.send_message(f"Added keyword: **{keyword}**")
    else:
        error_msg = result.get('error', 'unknown')
        await interaction.response.send_message(f"Failed to add keyword! Error: {error_msg}")
        logger.warning(f"Failed /add command: {error_msg}")

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

client.run(TOKEN)