import os
from dotenv import load_dotenv
import discord
from discord import app_commands
import requests
import time
from db import init_db, get_random_response, get_all_responses

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
    await tree.sync()  # Sync commands with Discord
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

client.run(TOKEN)
