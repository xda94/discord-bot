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
import requests
from bs4 import BeautifulSoup
import db
from moods import (
    COOLDOWN, TEASE_BASE_CHANCE, TEASE_MOODS,
    INACTIVITY_THRESHOLD, INACTIVITY_MESSAGES,
)
from logger import setup_logger

logger = setup_logger("discord_bot", "bot.log")

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
SPONSOR_PASSWORD = os.getenv('SPONSOR_PASSWORD')

SPONSOR_TIERS = {
    "standard": {"name": "Sponsor Standard", "price": "6 lei / an", "chance": 0.01},
    "entuziast": {"name": "Sponsor Entuziast", "price": "8 lei / an", "chance": 0.03},
    "premium": {"name": "Sponsor Premium", "price": "10 lei / an", "chance": 0.05},
    "ultra": {"name": "Sponsor Ultra Pro Max", "price": "20 lei / an", "chance": 0.08},
}

SPONSOR_TIER_CHOICES = [
    app_commands.Choice(name=t["name"], value=k) for k, t in SPONSOR_TIERS.items()
]

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
    joke_last_sent_date = None
    sponsor = None
    sponsor_set_at = None
    sponsor_warned = False
    sponsor_tier = "standard"
    sponsor_custom_message = None


state = BotState()

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

db.init_db()

# Load persisted joke settings from DB
_joke_settings = db.get_joke_settings()
state.joke_channel_id = _joke_settings["channel_id"]
state.joke_send_time = _joke_settings["send_time"]
_joke_last_sent = db.get_setting("joke_last_sent_date")
state.joke_last_sent_date = date.fromisoformat(_joke_last_sent) if _joke_last_sent else None
state.sponsor = db.get_setting("sponsor")
_sponsor_set_at = db.get_setting("sponsor_set_at")
state.sponsor_set_at = float(_sponsor_set_at) if _sponsor_set_at else None
_sponsor_tier = db.get_setting("sponsor_tier")
state.sponsor_tier = _sponsor_tier if _sponsor_tier in SPONSOR_TIERS else "standard"
state.sponsor_custom_message = db.get_setting("sponsor_custom_message") or None

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
        if not check_sponsor_expiry.is_running():
            check_sponsor_expiry.start()
        if not scrape_price_task.is_running():
            scrape_price_task.start()
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
        responses_data = db.get_all_responses()

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
                if state.sponsor:
                    tier = SPONSOR_TIERS.get(state.sponsor_tier, SPONSOR_TIERS["standard"])
                    if random.random() < tier["chance"]:
                        if state.sponsor_tier == "ultra" and state.sponsor_custom_message:
                            new_response += f" ({state.sponsor_custom_message})"
                        else:
                            new_response += f" (Sponsored by {state.sponsor})"
                await message.reply(new_response, mention_author=False)
                
                state.last_response_time = now
                if message.guild:
                    db.log_keyword_usage(keyword, message.author.id, message.guild.id)
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

@tree.command(name="keyword_add", description="Add a new keyword and response")
@app_commands.describe(keyword="The keyword", response="The response")
async def keyword_add(interaction: discord.Interaction, keyword: str, response: str):
    logger.info(f"Command /keyword_add called by {interaction.user} for '{keyword}'")
    db.add_response(keyword, response)
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
        due = db.get_due_reminders()
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
                db.delete_reminder(rem_id)
                
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
    db.add_reminder(who.id, interaction.channel_id, remind_at, what)
    
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
        rows = db.get_top_keywords_by_user(guild_id, user.id)
        title = f"Top keywords for {user.display_name}"
    else:
        rows = db.get_top_keywords(guild_id)
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

class SponsorModal(discord.ui.Modal, title="Set Sponsor"):
    password = discord.ui.TextInput(label="Password", placeholder="Enter the password", max_length=100)
    custom_message = discord.ui.TextInput(
        label="Custom message (Ultra Pro Max only)",
        placeholder="Leave empty if not Ultra Pro Max",
        required=False,
        max_length=200,
    )

    def __init__(self, sponsor_name: str = None, tier: str = "standard"):
        super().__init__()
        self.sponsor_name = sponsor_name
        self.tier = tier

    async def on_submit(self, interaction: discord.Interaction):
        if self.password.value != SPONSOR_PASSWORD:
            await interaction.response.send_message("Wrong password.", ephemeral=True)
            return
        sponsor_name = self.sponsor_name
        tier = self.tier
        logger.info(f"Command /sponsor_set called by {interaction.user} with name={sponsor_name}, tier={tier}")
        state.sponsor = sponsor_name
        if sponsor_name:
            db.set_setting("sponsor", sponsor_name)
            state.sponsor_set_at = time.time()
            state.sponsor_warned = False
            state.sponsor_tier = tier
            db.set_setting("sponsor_set_at", str(state.sponsor_set_at))
            db.set_setting("sponsor_tier", tier)
            if tier == "ultra" and self.custom_message.value:
                state.sponsor_custom_message = self.custom_message.value
                db.set_setting("sponsor_custom_message", self.custom_message.value)
            else:
                state.sponsor_custom_message = None
                db.set_setting("sponsor_custom_message", "")
        else:
            db.set_setting("sponsor", "")
            state.sponsor_set_at = None
            state.sponsor_warned = False
            state.sponsor_tier = "standard"
            state.sponsor_custom_message = None
            db.set_setting("sponsor_set_at", "")
            db.set_setting("sponsor_tier", "")
            db.set_setting("sponsor_custom_message", "")
        tier_info = SPONSOR_TIERS.get(tier, SPONSOR_TIERS["standard"])
        if sponsor_name:
            await interaction.response.send_message(
                f"Sponsor set to **{sponsor_name}** with plan **{tier_info['name']}**.", ephemeral=True
            )
        else:
            await interaction.response.send_message("Sponsor cleared.", ephemeral=True)

@tree.command(name="sponsor_set", description="Set or clear the sponsor tag")
@app_commands.describe(user="Select the sponsor user (omit to clear)", plan="Sponsorship plan")
@app_commands.choices(plan=SPONSOR_TIER_CHOICES)
async def sponsor_set(interaction: discord.Interaction, user: discord.Member = None, plan: app_commands.Choice[str] = None):
    sponsor_name = user.display_name if user else None
    tier = plan.value if plan else "standard"
    await interaction.response.send_modal(SponsorModal(sponsor_name, tier))

@tasks.loop(hours=1)
async def check_sponsor_expiry():
    try:
        if not state.sponsor or not state.sponsor_set_at:
            return

        elapsed = time.time() - state.sponsor_set_at
        one_year = 365 * 24 * 3600
        one_day_before = one_year - 24 * 3600

        # Warn 1 day before expiry
        if elapsed >= one_day_before and not state.sponsor_warned:
            state.sponsor_warned = True
            for guild in client.guilds:
                channel = guild.system_channel or next(
                    (ch for ch in guild.text_channels if ch.permissions_for(guild.me).send_messages), None
                )
                if channel:
                    await channel.send(
                        f"@everyone Sponsorship for **{state.sponsor}** is going to expire in one day. "
                        f"Who would like to be the next sponsor?"
                    )
            logger.info(f"Sponsor expiry warning sent for '{state.sponsor}'")

        # Expire after 1 year
        if elapsed >= one_year:
            logger.info(f"Sponsor '{state.sponsor}' has expired")
            state.sponsor = None
            state.sponsor_set_at = None
            state.sponsor_warned = False
            db.set_setting("sponsor", "")
            db.set_setting("sponsor_set_at", "")
    except Exception:
        logger.exception("Error in check_sponsor_expiry task")

@tree.command(name="sponsor_plans", description="Show available sponsorship plans")
async def sponsor_plans(interaction: discord.Interaction):
    logger.info(f"Command /sponsor_plans called by {interaction.user}")
    text = (
        "**Available Sponsorship Plans:**\n\n"
        "**Sponsor Standard** — 6 lei / an — 1% sansa sa adauge la un raspuns `(Sponsored by @User)`\n"
        "**Sponsor Entuziast** — 8 lei / an — 3% sansa sa adauge la un raspuns `(Sponsored by @User)`\n"
        "**Sponsor Premium** — 10 lei / an — 5% sansa sa adauge la un raspuns `(Sponsored by @User)`\n"
        "**Sponsor Ultra Pro Max** — 20 lei / an — 8% sansa sa adauge la un raspuns un mesaj pe care il vrei tu"
    )
    await interaction.response.send_message(text)

@tree.command(name="sponsor_who", description="Show the current sponsor and time until expiry")
async def sponsor_who(interaction: discord.Interaction):
    logger.info(f"Command /sponsor_who called by {interaction.user}")
    if not state.sponsor or not state.sponsor_set_at:
        await interaction.response.send_message("There is no active sponsor right now.", ephemeral=True)
        return

    elapsed = time.time() - state.sponsor_set_at
    one_year = 365 * 24 * 3600
    remaining = one_year - elapsed

    if remaining <= 0:
        await interaction.response.send_message("The sponsorship has expired.", ephemeral=True)
        return

    days = int(remaining // 86400)
    hours = int((remaining % 86400) // 3600)
    minutes = int((remaining % 3600) // 60)

    tier_info = SPONSOR_TIERS.get(state.sponsor_tier, SPONSOR_TIERS["standard"])
    await interaction.response.send_message(
        f"**Current Sponsor:** {state.sponsor}\n"
        f"**Plan:** {tier_info['name']}\n"
        f"**Expires in:** {days}d {hours}h {minutes}m"
    )

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
        "**/keyword_add** `<keyword>` `<response>`\n"
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
        "**/joke_add** `<text>`\n"
        "Add a joke/text to the daily joke list. "
        "Once all jokes are sent, the cycle resets.\n\n"
        "**/joke_activation** `<time>`\n"
        "Activate the daily joke in this channel at the specified time (e.g. `14:00`).\n\n"
        "**/sponsor_plans**\n"
        "Show the available sponsorship plans and pricing.\n\n"
        "**/sponsor_who**\n"
        "Show who the current sponsor is, their plan, and how much time remains until expiry.\n\n"
        "**/stats**\n"
        "Show hardware stats: CPU, RAM, disk, temperature, network, uptime, and bot memory usage.\n\n"
        "**/help**\n"
        "Show this message."
    )
    await interaction.response.send_message(text, ephemeral=True)

@tree.command(name="joke_add", description="Add a joke/text to the daily joke list")
@app_commands.describe(text="The joke or text to add")
async def joke_add(interaction: discord.Interaction, text: str):
    logger.info(f"Command /joke_add called by {interaction.user}")
    db.add_joke(text)
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
    db.set_setting("joke_send_time", time)
    db.set_setting("joke_channel_id", interaction.channel_id)
    await interaction.response.send_message(
        f"Daily joke activated in this channel at **{time}** every day."
    )

@tasks.loop(seconds=30)
async def daily_joke_check():
    try:
        now = datetime.now()
        today = now.date()
        target = datetime.strptime(state.joke_send_time, "%H:%M").replace(
            year=now.year, month=now.month, day=now.day
        )

        # Already sent today
        if state.joke_last_sent_date == today:
            return

        # Check if it's within the send window
        if not (target <= now <= target + timedelta(minutes=2)):
            return
        if state.joke_channel_id is None:
            logger.warning("No joke channel set. Use /joke_activation to set one.")
            return

        result = db.get_unsent_joke()
        if result is None:
            db.reset_jokes()
            result = db.get_unsent_joke()
            if result is None:
                logger.info("No jokes in database. Skipping daily joke.")
                return

        joke_id, text = result
        channel = client.get_channel(state.joke_channel_id)
        if channel:
            await channel.send(f"**Joke of the day:**\n{text}")
            db.mark_joke_sent(joke_id)
            state.joke_last_sent_date = today
            db.set_setting("joke_last_sent_date", today.isoformat())
            logger.info(f"Daily joke sent: ID {joke_id}")
        else:
            logger.warning(f"Joke channel {state.joke_channel_id} not accessible")
    except Exception:
        logger.exception("Error in daily_joke_check task")

@tree.command(name="scrape-item", description="Add a link to track price and stock")
@app_commands.describe(url="The URL of the item to track")
async def scrape_item(interaction: discord.Interaction, url: str):
    logger.info(f"Command /scrape-item called by {interaction.user} for {url}")
    db.add_scraped_item(interaction.user.id, url)
    await interaction.response.send_message(f"I've added the link to your tracking list! I'll check it every 12 hours.", ephemeral=True)

@tree.command(name="scrape-item-delete", description="Remove a link from tracking")
@app_commands.describe(url="The URL to remove")
async def scrape_item_delete(interaction: discord.Interaction, url: str):
    logger.info(f"Command /scrape-item-delete called by {interaction.user} for {url}")
    success = db.delete_scraped_item(interaction.user.id, url)
    if success:
        await interaction.response.send_message(f"Link removed and data cleared.", ephemeral=True)
    else:
        await interaction.response.send_message(f"Link not found in your list.", ephemeral=True)

def get_price_and_stock(url):
    """
    Placeholder function for web scraping. 
    Needs customization based on the target website's HTML structure.
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return None, False
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # EXEMPLU GENERIC (trebuie adaptat pentru fiecare site):
        # Încearcă să găsească prețul în meta tags sau clase comune
        price = None
        price_meta = soup.find("meta", property="product:price:amount") or soup.find("meta", property="og:price:amount")
        if price_meta:
            price = float(price_meta["content"].replace(',', '.'))
        
        # Verificare sumară stoc (exemplu: caută textul "în stoc")
        in_stock = "in stoc" in response.text.lower() or "în stoc" in response.text.lower()
        
        return price, in_stock
    except Exception as e:
        logger.error(f"Scraping error for {url}: {e}")
        return None, False

@tasks.loop(hours=12)
async def scrape_price_task():
    logger.info("Starting scheduled price scrape task...")
    items = db.get_all_scraped_items()
    
    for item_id, user_id, url, old_price, old_stock_status in items:
        new_price, is_in_stock = get_price_and_stock(url)
        
        if new_price is None:
            continue
        
        # Store price in history
        db.add_price_history(item_id, new_price)
        
        # Logic for notifications
        price_changed = old_price is not None and new_price != old_price
        back_in_stock = not old_stock_status and is_in_stock
        
        if price_changed or back_in_stock:
            try:
                user = await client.fetch_user(user_id)
                if user:
                    msg = f"🔔 **Update for your tracked item!**\nLink: {url}\n"
                    if back_in_stock:
                        msg += "✅ Item is now **BACK IN STOCK**!\n"
                    if price_changed:
                        msg += f"💰 Price changed: `{old_price}` -> **{new_price}**\n"
                    
                    await user.send(msg)
                    logger.info(f"Price alert sent to user {user_id} for {url}")
            except Exception as e:
                logger.error(f"Could not send DM to user {user_id}: {e}")

        # Update current state in DB
        db.update_scraped_item_status(item_id, new_price, is_in_stock)
        
    # Cleanup history older than 5 days
    db.clean_old_price_history(days=5)
    logger.info("Finished price scrape task and cleaned history.")

client.run(TOKEN)
