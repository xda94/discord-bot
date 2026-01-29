import os
from dotenv import load_dotenv
import discord
from discord import app_commands
import requests
import time
from db import init_db, get_random_response, get_all_responses, add_reminder, get_due_reminders, delete_reminder
import re
from discord.ext import tasks
import time

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
HOST = os.getenv('HOST')
PORT = os.getenv('PORT')

API_URL = f"http://{HOST}:{PORT}/add"  # Your Flask API

intents = discord.Intents.default()
intents.message_content = True

last_response_time = 0
COOLDOWN = 10  # seconds

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
init_db()

# Function to call API
def add_keyword_response(keyword, response):
    payload = {"keyword": keyword, "response": response}
    try:
        r = requests.post(API_URL, json=payload)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        print("API error:", e)
        return {"error": str(e)}

@client.event
async def on_ready():
    # This syncs your commands so they appear in Discord
    await tree.sync()
    if not check_reminders.is_running():
        check_reminders.start()
    print(f"Logged in as {client.user}")

@client.event
async def on_message(message):
    global last_response_time

    if message.author.bot:
        return

    now = time.time()
    if now - last_response_time < COOLDOWN:
        return  # still in cooldown, ignore this message

    content = message.content.lower()
    all_keywords = get_all_responses().keys()

    for keyword in all_keywords:
        if keyword in content:
            response = get_random_response(keyword)
            if response:
                await message.reply(response, mention_author=False)
                last_response_time = now  # update the timestamp
                break

# Slash command to add a keyword
@tree.command(name="add", description="Add a new keyword and response")
@app_commands.describe(keyword="The keyword to trigger the response", response="The response for the keyword")
async def add(interaction: discord.Interaction, keyword: str, response: str):
    result = add_keyword_response(keyword, response)
    if "status" in result and result["status"] == "ok":
         await interaction.response.send_message(f"Added keyword!")
    else:
        await interaction.response.send_message(f"Failed to add keyword! Error: {result.get('error', 'unknown')}")

def parse_time(time_str):
    minutes_per_unit = {"m": 1, "h": 60, "d": 1440}
    match = re.match(r"(\d+)([mhd])", time_str.lower())
    if not match:
        return None
    amount, unit = match.groups()
    return int(amount) * minutes_per_unit[unit] * 60

# Background task to check reminders
@tasks.loop(seconds=10)
async def check_reminders():
    due = get_due_reminders()
    for rem_id, user_id, channel_id, message in due:
        channel = client.get_channel(channel_id)
        if channel:
            await channel.send(f"🔔 <@{user_id}>, here is your reminder: **{message}**")
        delete_reminder(rem_id)

# The /remind command
@tree.command(name="remind", description="Set a reminder")
@app_commands.describe(
    when="Time until reminder (e.g. 30m, 1h, 1d)", 
    who="The user to remind", 
    what="What to remind them about"
)
async def remind(interaction: discord.Interaction, when: str, who: discord.Member, what: str):
    seconds = parse_time(when)
    if seconds is None:
        await interaction.response.send_message("Invalid time format! Use 1m, 1h, or 1d.", ephemeral=True)
        return

    remind_at = time.time() + seconds
    add_reminder(who.id, interaction.channel_id, remind_at, what)
    
    await interaction.response.send_message(f"Got it! I'll remind {who.display_name} about '{what}' in {when}.")

client.run(TOKEN)
