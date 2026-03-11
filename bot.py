import os
import re
import time
import random
from datetime import date, datetime, timedelta
import psutil
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from db import (
    init_db, add_response, get_all_responses,
    add_reminder, get_due_reminders, delete_reminder,
    log_keyword_usage, get_top_keywords, get_top_keywords_by_user,
    add_joke, get_unsent_joke, mark_joke_sent, reset_jokes
)
from moods import (
    COOLDOWN, TEASE_BASE_CHANCE, TEASE_MOODS,
    INACTIVITY_THRESHOLD, INACTIVITY_MESSAGES,
)
from logger import setup_logger

logger = setup_logger("discord_bot", "bot.log")

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')

intents = discord.Intents.default()
intents.message_content = True


class BotState:
    last_response_time = 0
    last_used_responses = {}
    teases_today = 0
    tease_reset_date = None
    current_mood = "bad"
    inactivity_state = {}
    joke_channel_id = None
    joke_send_time = "12:00"
    joke_sent_today = False


state = BotState()

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

init_db()

@client.event
async def on_ready():
    try:
        await tree.sync()
        if not check_reminders.is_running():
            check_reminders.start()
        if not check_inactivity.is_running():
            check_inactivity.start()
        if not daily_joke_check.is_running():
            daily_joke_check.start()
        logger.info(f"Bot is ready! Logged in as {client.user} (ID: {client.user.id})")
    except Exception:
        logger.exception("Error during on_ready startup sequence")

@client.event
async def on_message(message):
    if message.author.bot:
        return

    if message.guild:
        state.inactivity_state[message.guild.id] = {
            "last_time": time.time(),
            "channel_id": message.channel.id,
        }

    now = time.time()
    if now - state.last_response_time < COOLDOWN:
        return

    content = message.content.lower()
    
    try:
        responses_data = get_all_responses()

        for keyword in responses_data:
            if re.search(r'(?<!\w)' + re.escape(keyword) + r'(?!\w)', content):
                options = responses_data[keyword]
                if not options:
                    continue

                new_response = random.choice(options)
                if len(options) > 1:
                    last_one = state.last_used_responses.get(keyword)
                    while new_response == last_one:
                        new_response = random.choice(options)

                state.last_used_responses[keyword] = new_response
                await message.reply(new_response, mention_author=False)
                
                state.last_response_time = now
                if message.guild:
                    log_keyword_usage(keyword, message.author.id, message.guild.id)
                logger.info(f"Triggered response for '{keyword}' in #{message.channel}")
                return

    except Exception:
        logger.exception("Error processing message for keywords")

    # Random tease — decaying chance so it doesn't spam on busy days
    today = date.today()
    if state.tease_reset_date != today:
        state.teases_today = 0
        state.tease_reset_date = today
    chance = TEASE_BASE_CHANCE / (1 + state.teases_today)
    if random.random() < chance:
        tease = random.choice(TEASE_MOODS[state.current_mood]).format(user=message.author.display_name)
        await message.reply(tease, mention_author=False)
        state.teases_today += 1
        logger.info(f"Tease #{state.teases_today} triggered on {message.author} in #{message.channel}")

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

@tasks.loop(minutes=30)
async def check_inactivity():
    try:
        now = time.time()
        for guild_id, guild_state in state.inactivity_state.items():
            if now - guild_state["last_time"] >= INACTIVITY_THRESHOLD:
                channel = client.get_channel(guild_state["channel_id"])
                if channel:
                    msg = random.choice(INACTIVITY_MESSAGES)
                    await channel.send(msg)
                    guild_state["last_time"] = now
                    logger.info(f"Inactivity nudge sent in #{channel} (guild {guild_id})")
    except Exception:
        logger.exception("Error in check_inactivity loop")

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

MOOD_CHOICES = [
    app_commands.Choice(name=m, value=m) for m in TEASE_MOODS
] + [app_commands.Choice(name="random", value="random")]

@tree.command(name="mood", description="Set the bot's mood")
@app_commands.describe(mood="The mood to set")
@app_commands.choices(mood=MOOD_CHOICES)
async def mood(interaction: discord.Interaction, mood: app_commands.Choice[str]):
    chosen = mood.value
    if chosen == "random":
        chosen = random.choice(list(TEASE_MOODS.keys()))
    logger.info(f"Command /mood called by {interaction.user} — setting mood to {chosen}")
    state.current_mood = chosen
    state.teases_today = 0
    await interaction.response.send_message(f"Mood set to **{mood.value}**.")

@tree.command(name="stats", description="Show hardware stats for the machine running the bot")
async def stats(interaction: discord.Interaction):
    logger.info(f"Command /stats called by {interaction.user}")

    # CPU
    cpu_percent = psutil.cpu_percent(interval=1)
    cpu_freq = psutil.cpu_freq()
    freq_str = f"{cpu_freq.current:.0f}MHz" if cpu_freq else "N/A"
    load_1, load_5, load_15 = os.getloadavg()

    # Memory
    mem = psutil.virtual_memory()
    mem_used = mem.used / (1024 ** 2)
    mem_total = mem.total / (1024 ** 2)

    # Disk
    disk = psutil.disk_usage("/")
    disk_used = disk.used / (1024 ** 2)
    disk_total = disk.total / (1024 ** 2)

    # Temperature
    temps = psutil.sensors_temperatures()
    if temps:
        first_sensor = next(iter(temps.values()))
        temp_str = f"{first_sensor[0].current:.1f}°C"
    else:
        temp_str = "N/A"

    # Uptime
    boot_time = psutil.boot_time()
    uptime = timedelta(seconds=time.time() - boot_time)
    days = uptime.days
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes = remainder // 60
    uptime_str = f"{days}d {hours}h {minutes}m"

    # Network
    net = psutil.net_io_counters()
    net_sent = net.bytes_sent / (1024 ** 3)
    net_recv = net.bytes_recv / (1024 ** 3)

    # Bot process
    proc = psutil.Process()
    bot_mem = proc.memory_info().rss / (1024 ** 2)

    text = (
        "**System Stats**\n"
        f"🌡️ Temperature: {temp_str}\n"
        f"🖥️ CPU: {cpu_percent}% @ {freq_str} | Load: {load_1:.2f} / {load_5:.2f} / {load_15:.2f}\n"
        f"🧠 RAM: {mem_used:.0f} / {mem_total:.0f} MB ({mem.percent}%)\n"
        f"💾 Disk: {disk_used:.0f} / {disk_total:.0f} MB ({disk.percent}%)\n"
        f"🌐 Network: ↑ {net_sent:.2f} GB / ↓ {net_recv:.2f} GB\n"
        f"⏱️ Uptime: {uptime_str}\n"
        f"🤖 Bot memory: {bot_mem:.1f} MB"
    )
    await interaction.response.send_message(text)

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
        "Set the bot's mood. Changes the style of random tease messages. Default mood is `bad`."
        "Moods: `bad`, `good`, `computer`, "
        "`gen-z`, `dad`, `anime`, or `random`. "
        "Resets the tease counter for the day.\n\n"
        "**/joke** `<text>`\n"
        "Add a joke/text to the daily joke list. "
        "Once all jokes are sent, the cycle resets.\n\n"
        "**/joke_activation** `<time>`\n"
        "Activate the daily joke in this channel at the specified time (e.g. `14:00`).\n\n"
        "**/stats**\n"
        "Show hardware stats: CPU, RAM, disk, temperature, network, uptime, and bot memory usage.\n\n"
        "**/help**\n"
        "Show this message."
    )
    await interaction.response.send_message(text, ephemeral=True)

@tree.command(name="joke", description="Add a joke/text to the daily joke list")
@app_commands.describe(text="The joke or text to add")
async def joke(interaction: discord.Interaction, text: str):
    logger.info(f"Command /joke called by {interaction.user}")
    add_joke(text)
    await interaction.response.send_message(f"Joke added!")

@tree.command(name="joke_activation", description="Activate the daily joke in this channel at a specific time")
@app_commands.describe(time="The time to send the daily joke (e.g. 14:00)")
async def joke_activation(interaction: discord.Interaction, time: str):
    logger.info(f"Command /joke_activation called by {interaction.user} with time {time}")
    try:
        datetime.strptime(time, "%H:%M")
    except ValueError:
        await interaction.response.send_message("Invalid time format! Use HH:MM (e.g. `14:00`).", ephemeral=True)
        return
    state.joke_send_time = time
    state.joke_channel_id = interaction.channel_id
    await interaction.response.send_message(
        f"Daily joke activated in this channel at **{time}** every day."
    )

@tasks.loop(seconds=30)
async def daily_joke_check():
    try:
        now = datetime.now()
        target = datetime.strptime(state.joke_send_time, "%H:%M").replace(
            year=now.year, month=now.month, day=now.day
        )

        # Reset flag after the send window passes (2 minutes after target)
        if now > target + timedelta(minutes=2):
            state.joke_sent_today = False if now > target + timedelta(hours=1) else state.joke_sent_today

        # Reset flag at midnight
        if now.hour == 0 and now.minute == 0:
            state.joke_sent_today = False

        # Check if it's time and we haven't sent yet
        if state.joke_sent_today:
            return
        if not (target <= now <= target + timedelta(minutes=2)):
            return
        if state.joke_channel_id is None:
            logger.warning("No joke channel set. Use /joke_activation to set one.")
            return

        result = get_unsent_joke()
        if result is None:
            reset_jokes()
            result = get_unsent_joke()
            if result is None:
                logger.info("No jokes in database. Skipping daily joke.")
                return

        joke_id, text = result
        channel = client.get_channel(state.joke_channel_id)
        if channel:
            await channel.send(f"**Joke of the day:**\n{text}")
            mark_joke_sent(joke_id)
            state.joke_sent_today = True
            logger.info(f"Daily joke sent: ID {joke_id}")
        else:
            logger.warning(f"Joke channel {state.joke_channel_id} not accessible")
    except Exception:
        logger.exception("Error in daily_joke_check task")

client.run(TOKEN)
