import asyncio
import random
import time
from types import SimpleNamespace

import discord
from discord import app_commands
from discord.ext import tasks

from analytics import record
from db import community
from features.humor_common import interaction_language, safe_text, text


STRINGS = {
    "invalid": ("Choose a human member's text message from this server, with 1–1,200 characters. System and attachment-only messages cannot be nominated.", "Alege un mesaj text al unui membru uman de pe acest server, cu 1–1.200 de caractere. Mesajele de sistem și cele doar cu atașamente nu pot fi nominalizate."),
    "human": ("Only human members of this server can use this action.", "Doar membrii umani ai acestui server pot folosi această acțiune."),
    "nominated": ("I sent the author a private consent request. The quote stays unavailable until they accept within 24 hours.", "Am trimis autorului o cerere privată de acord. Citatul rămâne indisponibil până când acceptă în 24 de ore."),
    "duplicate": ("This message already has a museum nomination or approval.", "Acest mesaj are deja o nominalizare sau aprobare în muzeu."),
    "consent": ("🏛️ Quote Museum · #{id}\nThe preceding message is the complete quote snapshot from server {guild}, source channel ID {channel}.\nAccepting makes it available throughout that server, including to members who cannot see the source channel. Consent is for this quote only and expires after 24 hours: {deadline}.\nYou must still be a server member to accept. You can refuse now or withdraw later with the removal button, even after leaving the server. Deletion removes museum availability and active quizzes; it cannot recall screenshots or previously sent ordinary Discord messages.", "🏛️ Muzeul citatelor · #{id}\nMesajul precedent este copia completă a citatului de pe serverul {guild}, canalul sursă cu ID {channel}.\nAcceptarea îl face disponibil pe întregul server, inclusiv membrilor care nu pot vedea canalul sursă. Acordul este doar pentru acest citat și expiră după 24 de ore: {deadline}.\nTrebuie să fii încă membru al serverului pentru a accepta. Poți refuza acum sau retrage ulterior cu butonul de ștergere, chiar după părăsirea serverului. Ștergerea elimină disponibilitatea în muzeu și quizurile active; nu poate retrage capturi de ecran sau mesaje Discord obișnuite trimise anterior."),
    "approved": ("Quote #{id} is approved for the entire source server. Only you can use this button to withdraw it, even after leaving. Deletion removes museum availability and active quizzes; it cannot recall screenshots or previously sent ordinary Discord messages.", "Citatul #{id} este aprobat pentru întregul server sursă. Doar tu poți folosi acest buton pentru a-l retrage, chiar după părăsirea serverului. Ștergerea elimină disponibilitatea în muzeu și quizurile active; nu poate retrage capturi de ecran sau mesaje Discord obișnuite trimise anterior."),
    "deleted": ("The quote was erased from the museum and related quizzes were revoked. Previously sent ordinary messages and screenshots cannot be recalled.", "Citatul a fost șters din muzeu și quizurile asociate au fost revocate. Mesajele obișnuite deja trimise și capturile de ecran nu pot fi retrase."),
    "declined": ("Consent refused. The pending quote was erased.", "Acord refuzat. Citatul în așteptare a fost șters."),
    "membership": ("You must still belong to the source server to approve this quote.", "Trebuie să fii încă membru al serverului sursă pentru a aproba acest citat."),
    "empty": ("There are no approved quotes in this server's museum.", "Nu există citate aprobate în muzeul acestui server."),
    "pool": ("A quiz needs approved quotes from at least two distinct authors in this server.", "Un quiz necesită citate aprobate de la cel puțin doi autori diferiți de pe acest server."),
    "forbidden": ("Only the quote author or a member with Manage Messages can delete it.", "Doar autorul citatului sau un membru cu permisiunea Gestionare mesaje îl poate șterge."),
    "quote": ("🏛️ Quote #{id} · {author}\n", "🏛️ Citat #{id} · {author}\n"),
    "published": ("Published in this channel.", "Am publicat în acest canal."),
}


def _t(key, language, **values):
    return STRINGS[key][language == "ro"].format(**values)


class QuoteConsentView(discord.ui.View):
    def __init__(self, feature, quote, disabled=False):
        super().__init__(timeout=None)
        actions = ("remove",) if quote["status"] == "approved" else ("accept", "decline")
        for action in actions:
            button = discord.ui.Button(label=text(action, quote["language"]), custom_id=f"museum:{quote['id']}:{action}", disabled=disabled)

            async def callback(interaction, selected=action):
                await feature.handle_control(interaction, quote["guild_id"], quote["id"], selected)

            button.callback = callback
            self.add_item(button)


class QuoteMuseumFeature:
    def __init__(self, client, tree, games):
        self.client = client
        self.tree = tree
        self.games = games
        self._startup_lock = None
        self._locks = {}
        self._restored = set()
        self._register_commands()

    def _lock(self, quote_id):
        if quote_id not in self._locks:
            self._locks[quote_id] = asyncio.Lock()
        return self._locks[quote_id]

    async def _record(self, activity, guild_id, category="failure"):
        await record(category, "museum/" + activity, guild_id=guild_id, scope_type="guild")

    async def _defer(self, interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

    async def _reply(self, interaction, content):
        await interaction.followup.send(content, ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)

    async def _admit(self, interaction, language):
        if interaction.guild_id is None:
            await self._reply(interaction, text("guild_only", language))
            return False
        if getattr(interaction.guild, "id", None) != interaction.guild_id or getattr(interaction.user, "bot", True) or getattr(getattr(interaction.user, "guild", None), "id", None) != interaction.guild_id or getattr(interaction.channel, "id", None) != interaction.channel_id or getattr(getattr(interaction.channel, "guild", None), "id", None) != interaction.guild_id:
            await self._reply(interaction, _t("human", language))
            return False
        return True

    def _restore(self, quote):
        key = (quote["id"], quote["consent_message_id"], quote["status"])
        if quote["consent_message_id"] and key not in self._restored:
            self.client.add_view(QuoteConsentView(self, quote), message_id=quote["consent_message_id"])
            self._restored.add(key)

    async def nominate(self, interaction, message):
        await self._defer(interaction)
        language = await interaction_language(interaction)
        if not await self._admit(interaction, language):
            return None
        author = getattr(message, "author", None)
        content = getattr(message, "content", None)
        if getattr(getattr(message, "guild", None), "id", None) != interaction.guild_id or getattr(getattr(getattr(message, "channel", None), "guild", None), "id", None) != interaction.guild_id or getattr(author, "bot", True) or getattr(author, "system", False) or message.is_system() or not isinstance(content, str) or not content.strip() or len(content) > 1200:
            await self._reply(interaction, _t("invalid", language))
            return None
        quote = None
        sent = []
        try:
            author_language = await interaction_language(SimpleNamespace(user=author, locale=getattr(author, "locale", interaction.locale)))
            name = str(getattr(author, "display_name", None) or getattr(author, "name", None) or author.id).strip()[:100]
            quote = await asyncio.to_thread(community.create_quote_request, interaction.guild_id, message.channel.id, message.id, interaction.user.id, author.id, content, name, author_language)
            if not quote["created"]:
                await self._reply(interaction, _t("duplicate", language))
                return None
            sent.append(await author.send(quote["quote_text"], allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True))
            consent = _t("consent", author_language, id=quote["id"], guild=safe_text(getattr(interaction.guild, "name", interaction.guild_id), 200), channel=quote["source_channel_id"], deadline=f"<t:{int(quote['consent_expires_at'])}:F>")
            sent.append(await author.send(consent, view=QuoteConsentView(self, quote), allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True))
            result = await asyncio.to_thread(community.bind_quote_consent_message, quote["guild_id"], quote["id"], sent[-1].id, sent[-1].channel.id)
            if result["status"] != "bound":
                raise ValueError("bind_failed")
            quote = result["quote"]
        except Exception:
            if quote and quote.get("created"):
                try:
                    await asyncio.to_thread(community.delete_quote, quote["guild_id"], quote["id"], author_id=quote["author_id"])
                except Exception:
                    await self._record("erase", interaction.guild_id)
            for item in sent:
                try:
                    await item.delete()
                except Exception:
                    pass
            await self._record("nominate", interaction.guild_id)
            await self._reply(interaction, text("dm_failed" if isinstance(quote, dict) else "failed", language))
            return None
        try:
            self._restore(quote)
        except Exception:
            await self._record("restore", quote["guild_id"])
        await self._reply(interaction, _t("nominated", language))
        return quote

    async def _member(self, guild_id, author_id):
        guild = self.client.get_guild(guild_id)
        if guild is None:
            return False
        try:
            member = await guild.fetch_member(author_id)
        except discord.NotFound:
            return False
        return member.id == author_id and not member.bot

    async def _edit_control(self, interaction, quote, content, disabled=False):
        try:
            await interaction.message.edit(content=content, view=QuoteConsentView(self, quote, disabled), allowed_mentions=discord.AllowedMentions.none(), suppress=True)
            if not disabled:
                self._restore(quote)
        except Exception:
            await self._record("consent_edit", quote["guild_id"])
            if quote["status"] == "approved" and not disabled:
                try:
                    sent = await interaction.user.send(content, view=QuoteConsentView(self, quote), allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
                    result = await asyncio.to_thread(community.bind_quote_consent_message, quote["guild_id"], quote["id"], sent.id, sent.channel.id)
                    if result["quote"]:
                        self._restore(result["quote"])
                except Exception:
                    await self._record("removal_delivery", quote["guild_id"])

    async def handle_control(self, interaction, guild_id, quote_id, action):
        await self._defer(interaction)
        language = await interaction_language(interaction)
        async with self._lock(quote_id):
            try:
                quote = await asyncio.to_thread(community.get_quote, guild_id, quote_id)
                if not quote:
                    await self._reply(interaction, text("expired", language))
                    return
                if getattr(interaction.user, "bot", True) or interaction.user.id != quote["author_id"]:
                    await self._reply(interaction, text("not_owner", language))
                    return
                if interaction.guild_id is not None or getattr(interaction.message, "id", None) != quote["consent_message_id"] or interaction.channel_id != quote["consent_channel_id"]:
                    await self._reply(interaction, text("not_owner", language))
                    return
                if action not in (("remove",) if quote["status"] == "approved" else ("accept", "decline")):
                    if quote["status"] == "approved":
                        await self._edit_control(interaction, quote, _t("approved", quote["language"], id=quote["id"]))
                    await self._reply(interaction, text("expired", language))
                    return
                if action == "accept":
                    if not await self._member(guild_id, quote["author_id"]):
                        await self._reply(interaction, _t("membership", language))
                        return
                    result = await asyncio.to_thread(community.approve_quote, guild_id, quote_id, interaction.user.id)
                    if result["status"] != "approved":
                        await self._reply(interaction, text("expired", language))
                        return
                    changed = result["quote"]
                    response = _t("approved", language, id=quote_id)
                    await self._edit_control(interaction, changed, _t("approved", changed["language"], id=quote_id))
                else:
                    result = await asyncio.to_thread(community.delete_quote, guild_id, quote_id, author_id=interaction.user.id)
                    if result["status"] != "deleted":
                        await self._reply(interaction, text("expired", language))
                        return
                    response = _t("declined" if action == "decline" else "deleted", language)
                    await self._edit_control(interaction, quote, _t("declined" if action == "decline" else "deleted", quote["language"]), disabled=True)
                await self._record(action, guild_id, "control")
                await self._reply(interaction, response)
            except Exception:
                await self._record("control", guild_id)
                await self._reply(interaction, text("failed", language))

    async def quote(self, interaction):
        await self._defer(interaction)
        language = await interaction_language(interaction)
        if not await self._admit(interaction, language):
            return None
        try:
            quote = await asyncio.to_thread(community.choose_quote, interaction.guild_id)
            if not quote:
                await self._reply(interaction, _t("empty", language))
                return None
            quote = await asyncio.to_thread(community.get_quote, interaction.guild_id, quote["id"], approved_only=True)
            if not quote:
                await self._reply(interaction, _t("empty", language))
                return None
            header = _t("quote", language, id=quote["id"], author=safe_text(quote["author_name"], 200))
            await interaction.channel.send(header + quote["quote_text"], allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
            await self._reply(interaction, _t("published", language))
            return quote
        except Exception:
            await self._record("read", interaction.guild_id)
            await self._reply(interaction, text("failed", language))
            return None

    async def quiz(self, interaction):
        await self._defer(interaction)
        language = await interaction_language(interaction)
        if not await self._admit(interaction, language):
            return None
        try:
            quote = await asyncio.to_thread(community.choose_quote, interaction.guild_id)
            if not quote:
                await self._reply(interaction, _t("empty", language))
                return None
            authors = {quote["author_id"]: str(quote["author_name"] or quote["author_id"]).strip()}
            after_id = 0
            while len(authors) < 4:
                page = await asyncio.to_thread(community.list_approved_quotes, interaction.guild_id, after_id=after_id)
                for row in page:
                    name = str(row["author_name"] or "").strip()
                    if name and row["author_id"] not in authors:
                        authors[row["author_id"]] = name
                    if len(authors) == 4:
                        break
                if len(page) < 100:
                    break
                after_id = page[-1]["id"]
            if len(authors) < 2:
                await self._reply(interaction, _t("pool", language))
                return None
            candidates = [{"user_id": author_id, "name": name[:80]} for author_id, name in authors.items()]
            duplicate_names = {candidate["name"].casefold() for candidate in candidates if sum(other["name"].casefold() == candidate["name"].casefold() for other in candidates) > 1}
            for candidate in candidates:
                if duplicate_names:
                    suffix = f" ({candidate['user_id']})"
                    candidate["name"] = candidate["name"][:80 - len(suffix)] + suffix
            random.shuffle(candidates)
            return await self.games.create_quiz(interaction, quote, candidates)
        except Exception:
            await self._record("quiz", interaction.guild_id)
            await self._reply(interaction, text("failed", language))
            return None

    async def delete(self, interaction, quote_id):
        await self._defer(interaction)
        language = await interaction_language(interaction)
        if not await self._admit(interaction, language):
            return None
        try:
            quote = await asyncio.to_thread(community.get_quote, interaction.guild_id, quote_id)
            if not quote:
                await self._reply(interaction, text("expired", language))
                return None
            moderator = getattr(getattr(interaction.user, "guild_permissions", None), "manage_messages", False) is True
            if interaction.user.id != quote["author_id"] and not moderator:
                await self._reply(interaction, _t("forbidden", language))
                return None
            result = await asyncio.to_thread(community.delete_quote, interaction.guild_id, quote_id, author_id=None if moderator else interaction.user.id)
            await self._reply(interaction, _t("deleted", language) if result["status"] == "deleted" else text("expired", language))
            return result
        except Exception:
            await self._record("delete", interaction.guild_id)
            await self._reply(interaction, text("failed", language))
            return None

    async def _expire_requests(self):
        after_id = 0
        try:
            while True:
                page = await asyncio.to_thread(community.list_pending_quotes, now=0, after_id=after_id)
                for quote in page:
                    if quote["consent_expires_at"] > time.time():
                        continue
                    try:
                        current = await asyncio.to_thread(community.get_quote, quote["guild_id"], quote["id"])
                        if current or not quote["consent_message_id"]:
                            continue
                        channel = self.client.get_channel(quote["consent_channel_id"])
                        if channel is None:
                            channel = await self.client.fetch_channel(quote["consent_channel_id"])
                        message = await channel.fetch_message(quote["consent_message_id"])
                        await message.edit(content=text("expired", quote["language"]), view=QuoteConsentView(self, quote, disabled=True), allowed_mentions=discord.AllowedMentions.none(), suppress=True)
                    except Exception:
                        await self._record("expiry", quote["guild_id"])
                if len(page) < 100:
                    break
                after_id = page[-1]["id"]
        except Exception:
            await self._record("expiry", None)

    async def start_tasks(self):
        if self._startup_lock is None:
            self._startup_lock = asyncio.Lock()
        async with self._startup_lock:
            await self._expire_requests()
            after_id = 0
            try:
                while True:
                    page = await asyncio.to_thread(community.list_pending_quotes, after_id=after_id)
                    for quote in page:
                        try:
                            self._restore(quote)
                        except Exception:
                            await self._record("restore", quote["guild_id"])
                    if len(page) < 100:
                        break
                    after_id = page[-1]["id"]
            except Exception:
                await self._record("restore", None)
            after_guild_id = 0
            try:
                while True:
                    guilds = await asyncio.to_thread(community.list_quote_guilds, after_guild_id=after_guild_id)
                    for guild in guilds:
                        guild_id = guild["guild_id"]
                        after_id = 0
                        try:
                            while True:
                                page = await asyncio.to_thread(community.list_approved_quotes, guild_id, after_id=after_id)
                                for quote in page:
                                    try:
                                        self._restore(quote)
                                    except Exception:
                                        await self._record("restore", guild_id)
                                if len(page) < 100:
                                    break
                                after_id = page[-1]["id"]
                        except Exception:
                            await self._record("restore", guild_id)
                    if len(guilds) < 100:
                        break
                    after_guild_id = guilds[-1]["guild_id"]
            except Exception:
                await self._record("restore", None)
            if not self._check.is_running():
                self._check.start()

    @tasks.loop(seconds=15)
    async def _check(self):
        await self._expire_requests()

    def _register_commands(self):
        @app_commands.guild_only()
        async def nominate(interaction: discord.Interaction, message: discord.Message):
            await self.nominate(interaction, message)

        self.tree.add_command(app_commands.ContextMenu(name="Pune în muzeu", callback=nominate))

        @self.tree.command(name="citat", description="Citat aprobat din muzeul serverului / Approved quote from this server")
        @app_commands.guild_only()
        async def quote(interaction: discord.Interaction):
            await self.quote(interaction)

        @self.tree.command(name="cine-a-zis", description="Quiz cu autorii citați aprobați / Quiz with approved quote authors")
        @app_commands.guild_only()
        async def quiz(interaction: discord.Interaction):
            await self.quiz(interaction)

        @self.tree.command(name="citat-sterge", description="Retrage un citat propriu sau moderează / Withdraw or moderate a quote")
        @app_commands.guild_only()
        @app_commands.rename(quote_id="id")
        @app_commands.describe(quote_id="ID-ul citatului / Quote ID")
        async def delete(interaction: discord.Interaction, quote_id: int):
            await self.delete(interaction, quote_id)
