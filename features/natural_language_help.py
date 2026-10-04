"""Read-only, bilingual help for the deterministic mention commands."""
from __future__ import annotations

import discord


CATEGORY_LABELS = {
    "overview": ("Getting started", "Cum începi"),
    "reminders": ("Reminders", "Remindere"),
    "wishlist": ("Wishlist", "Lista de dorințe"),
    "prices": ("Prices and graphs", "Prețuri și grafice"),
    "flights": ("Saved flights", "Zboruri salvate"),
}

# Each example is published verbatim and checked against the deterministic
# parser in test_natural_language_help.py. URL and ID values are placeholders.
EXAMPLES = {
    "en": {
        "reminders": (
            "remind me in 20 minutes to stretch",
            "remind me tomorrow at 09:00 to call home",
            "remind me daily at 09:00 to drink water",
            "remind me weekdays at 09:00 to stretch",
            "remind me weekly at 09:00 to call home",
            "show my reminders",
            "cancel reminder 42",
            "snooze reminder 42",
        ),
        "wishlist": (
            "track https://example.com/product",
            "track this",
            "show my wishlist in EUR",
            "stop tracking 42",
            "set target price for 42 to 100 EUR",
            "clear target price for 42",
            "enable restock only for 42",
            "disable restock only for 42",
        ),
        "prices": (
            "refresh https://example.com/product",
            "refresh my wishlist",
            "graph https://example.com/product in EUR for 30 days",
            "graph my wishlist in EUR for 30 days",
            "compare my wishlist for 30 days",
        ),
        "flights": ("show my flights",),
    },
    "ro": {
        "reminders": (
            "amintește-mi peste 20 minute să fac mișcare",
            "amintește-mi mâine la 09:00 să sun acasă",
            "amintește-mi zilnic la 09:00 să beau apă",
            "amintește-mi în fiecare zi lucrătoare la 09:00 să fac mișcare",
            "amintește-mi săptămânal la 09:00 să sun acasă",
            "arată-mi reminderele mele",
            "anulează reminderul 42",
            "amână reminderul 42",
        ),
        "wishlist": (
            "urmărește https://example.com/product",
            "urmărește asta",
            "arată-mi lista de dorințe în EUR",
            "nu mai urmări 42",
            "setează prețul țintă pentru 42 la 100 EUR",
            "șterge prețul țintă pentru 42",
            "activează doar notificările de stoc pentru 42",
            "dezactivează doar notificările de stoc pentru 42",
        ),
        "prices": (
            "actualizează https://example.com/product",
            "actualizează wishlist-ul meu",
            "grafic pentru https://example.com/product în EUR pentru 30 zile",
            "grafic pentru wishlist-ul meu în EUR pentru 30 zile",
            "compară wishlist-ul meu pentru 30 zile",
        ),
        "flights": ("arată-mi zborurile mele",),
    },
}

PAGE_TEXT = {
    "en": {
        "overview": (
            "Mention the real bot account in Discord, then write one request in English or Romanian. "
            "Supported commands execute immediately; ✅ on your message means success. "
            "Other conversation goes to the normal assistant.\n\n"
            "Lists, product choices, refresh results and graphs arrive privately by DM. "
            "If DMs are blocked, use the private slash alternatives on each page. "
            "Reminder delivery uses its saved destination.\n\n"
            "Calendar and recurring reminders require a confirmed timezone: use /start or "
            "/assistant-profile-set. Relative reminders do not. Missing or ambiguous details "
            "open a clarification for ten minutes: mention the bot again in the same channel "
            "and supply the requested detail; reply to its question when requested.\n\n"
            "Choose a category below for supported examples."
        ),
        "reminders": (
            "Relative, calendar, daily, weekdays, weekly, list, cancel and snooze examples. "
            "Calendar/recurring times use your confirmed timezone (/start or /assistant-profile-set). "
            "Snooze adds ten minutes. Replace 42 with your own reminder ID from the private list.\n\n"
        ),
        "wishlist": (
            "Replace https://example.com/product with a real HTTP(S) product URL and 42 with "
            "your owned item ID from the private list. For the second example, reply to a "
            "message containing exactly one product URL. Existing items can also be identified "
            "by an unambiguous title; multiple matches open a private choice.\n\n"
        ),
        "prices": (
            "Replace the example URL with an owned tracked product URL. Refresh one item or "
            "all items; each item has a five-minute cooldown. Graph one item or your whole "
            "wishlist, or compare relative percentage changes.\n\n"
        ),
        "flights": (
            "Natural language only lists your saved flight trackers. Create and manage "
            "trackers with the slash commands below.\n\n"
        ),
    },
    "ro": {
        "overview": (
            "Menționează contul real al botului în Discord, apoi scrie o singură cerere în "
            "română sau engleză. Comenzile acceptate se execută imediat; ✅ pe mesajul tău "
            "înseamnă succes. Alte conversații ajung la asistentul obișnuit.\n\n"
            "Listele, selecțiile de produse, rezultatele actualizărilor și graficele vin "
            "privat prin DM. Dacă DM-urile sunt blocate, folosește alternativele slash "
            "private de pe fiecare pagină. Reminderele folosesc destinația salvată.\n\n"
            "Reminderele calendaristice și recurente necesită un fus orar confirmat: "
            "/start sau /assistant-profile-set. Cele relative nu necesită asta. Detaliile "
            "lipsă sau ambigue deschid o clarificare pentru zece minute: menționează din "
            "nou botul în același canal și oferă detaliul cerut; răspunde la întrebarea "
            "lui când îți cere.\n\n"
            "Alege o categorie mai jos pentru exemple acceptate."
        ),
        "reminders": (
            "Exemple relative, calendaristice, zilnice, în zile lucrătoare, săptămânale, "
            "listare, anulare și amânare. Orele calendaristice/recurente folosesc fusul "
            "orar confirmat (/start sau /assistant-profile-set). Amânarea adaugă zece "
            "minute. Înlocuiește 42 cu ID-ul reminderului tău din lista privată.\n\n"
        ),
        "wishlist": (
            "Înlocuiește https://example.com/product cu un URL HTTP(S) real de produs și "
            "42 cu ID-ul produsului tău din lista privată. Pentru al doilea exemplu, "
            "răspunde unui mesaj care conține exact un URL de produs. Produsele existente "
            "pot fi identificate și printr-un titlu neambiguu; mai multe potriviri deschid "
            "o selecție privată.\n\n"
        ),
        "prices": (
            "Înlocuiește URL-ul din exemplu cu URL-ul unui produs urmărit de tine. "
            "Actualizează un produs sau toate; fiecare produs are o pauză de cinci minute. "
            "Vezi graficul unui produs, al întregii liste sau compară variațiile procentuale.\n\n"
        ),
        "flights": (
            "Limbajul natural afișează doar trackerele de zbor salvate. Creează și "
            "gestionează trackere prin comenzile slash de mai jos.\n\n"
        ),
    },
}

SLASH_ALTERNATIVES = {
    "reminders": "/remind · /reminder-list · /reminder-edit · /reminder-cancel · /reminder-snooze",
    "wishlist": "/wishlist-item · /wishlist-show · /wishlist-item-delete · /wishlist-target-price · /wishlist-target-clear · /wishlist-restock-only",
    "prices": "/wishlist-refresh · /wishlist-graph · /wishlist-graph-all",
    "flights": "/flight-tracker-show · /flight-tracker-add · /flight-tracker-delete · /flight-tracker-budget · /flight-tracker-login · /flight-tracker-logout",
}


def display_language(interaction, profile=None):
    """Saved EN/RO preference, then Discord locale, then English; reads only."""
    if profile is None:
        import db
        profile = db.get_assistant_profile(interaction.user.id)
    saved = getattr(profile, "language", None)
    if saved is None and profile is not None:
        saved = profile.get("language")
    if saved in ("en", "ro"):
        return saved
    locale = str(getattr(interaction, "locale", "")).lower().replace("_", "-")
    return "ro" if locale.split("-")[0] == "ro" else "en"


def guide_content(language, category="overview", *, bot_mention=None):
    language = "ro" if language == "ro" else "en"
    category = category if category in CATEGORY_LABELS else "overview"
    mention = bot_mention or "@bot"
    label = CATEGORY_LABELS[category][language == "ro"]
    text = f"**/natural-language — {label}**\n\n" + PAGE_TEXT[language][category]
    if category == "overview":
        text += (f"\n\nMențiunea botului: {mention}" if language == "ro" else f"\n\nBot mention: {mention}")
    else:
        text += "\n".join(f"{mention} `{example}`" for example in EXAMPLES[language][category])
        text += "\n\n" + ("Alternative slash: " if language == "ro" else "Slash alternatives: ") + SLASH_ALTERNATIVES[category]
        if category in ("wishlist", "prices"):
            text += ("\nMonede: " if language == "ro" else "\nCurrencies: ") + "RON, DKK, EUR, USD, GBP."
        if category == "prices":
            text += (" Perioade: 1–180 zile întregi (implicit 180); comparația procentuală nu acceptă monedă." if language == "ro" else " Periods: 1–180 whole days (default 180); percentage comparison accepts no currency.")
    if not bot_mention:
        text += ("\n@bot este un substituent: selectează contul real al botului în Discord." if language == "ro" else "\n@bot is a placeholder: select the real bot account in Discord.")
    text += ("\n\nGhid privat; controalele expiră după zece minute. Limba schimbă doar ghidul." if language == "ro" else "\n\nPrivate guide; controls expire after ten minutes. Language changes only this guide.")
    return text


class NaturalLanguageHelpView(discord.ui.View):
    """One requester-owned response; navigation never changes profile state."""

    def __init__(self, user_id, language, *, bot_mention=None):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.language = "ro" if language == "ro" else "en"
        self.category = "overview"
        self.bot_mention = bot_mention
        self.category_select = discord.ui.Select(row=0)
        self.category_select.callback = self._select_category
        self.add_item(self.category_select)
        for code, label in (("en", "English"), ("ro", "Română")):
            button = discord.ui.Button(label=label, row=1)

            async def switch_language(interaction, selected=code):
                if not await self.interaction_check(interaction):
                    return
                self.language = selected
                await self._edit(interaction)

            button.callback = switch_language
            self.add_item(button)
        self._update_controls()

    def _update_controls(self):
        self.category_select.placeholder = "Alege categoria" if self.language == "ro" else "Choose a category"
        self.category_select.options = [
            discord.SelectOption(label=labels[self.language == "ro"], value=category, default=category == self.category)
            for category, labels in CATEGORY_LABELS.items()
        ]
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.label in ("English", "Română"):
                active = child.label == ("Română" if self.language == "ro" else "English")
                child.style = discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary

    async def _edit(self, interaction):
        self._update_controls()
        await interaction.response.edit_message(
            content=guide_content(self.language, self.category, bot_mention=self.bot_mention),
            view=self, allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _select_category(self, interaction):
        if not await self.interaction_check(interaction):
            return
        category = self.category_select.values[0]
        self.category = category if category in CATEGORY_LABELS else "overview"
        await self._edit(interaction)

    async def interaction_check(self, interaction):
        if interaction.user.id == self.user_id:
            return True
        content = "Aceste controale sunt pentru persoana care a deschis ghidul." if self.language == "ro" else "These controls belong to the person who opened this guide."
        await interaction.response.send_message(content, ephemeral=True)
        return False
