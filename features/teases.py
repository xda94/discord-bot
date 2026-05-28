import logging
import random
from datetime import date

import discord
from discord import app_commands

logger = logging.getLogger("discord_bot")

TEASE_BASE_CHANCE = 0.10

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
        "chat, is this real?",
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
        "You know, {user}, I was actually a pretty cool dad in my day",
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
        "{user} is the senpai of this server",
    ],
}

MOOD_CHOICES = [
    app_commands.Choice(name=m, value=m) for m in TEASE_MOODS
] + [app_commands.Choice(name="random", value="random")]


class TeasesFeature:
    """Random teases that fire on messages, plus the /mood command."""

    def __init__(self, client: discord.Client, tree: app_commands.CommandTree):
        self.client = client
        self.tree = tree
        self.current_mood = "bad"
        self.teases_today = 0
        self.tease_reset_date: date | None = None
        self._register_commands()

    async def handle_message(self, message: discord.Message) -> bool:
        today = date.today()
        if self.tease_reset_date != today:
            self.teases_today = 0
            self.tease_reset_date = today

        chance = TEASE_BASE_CHANCE / (1 + self.teases_today)
        if random.random() >= chance:
            return False

        tease = random.choice(TEASE_MOODS[self.current_mood]).format(
            user=message.author.display_name
        )
        try:
            await message.reply(tease, mention_author=False)
        except Exception:
            logger.exception("Failed to send tease")
            return False

        self.teases_today += 1
        logger.info(
            f"Tease #{self.teases_today} triggered on {message.author} in #{message.channel}"
        )
        # Teases historically did NOT consume the cooldown gate, so return False
        # so any other handlers (if added later) can still observe the message.
        return False

    def _register_commands(self) -> None:
        feature = self

        @self.tree.command(name="mood", description="Set the bot's mood")
        @app_commands.describe(mood="The mood to set")
        @app_commands.choices(mood=MOOD_CHOICES)
        async def mood(interaction: discord.Interaction, mood: app_commands.Choice[str]):
            chosen = mood.value
            if chosen == "random":
                chosen = random.choice(list(TEASE_MOODS.keys()))
            logger.info(
                f"Command /mood called by {interaction.user} — setting mood to {chosen}"
            )
            feature.current_mood = chosen
            feature.teases_today = 0
            await interaction.response.send_message(f"Mood set to **{mood.value}**.")
