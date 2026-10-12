import asyncio
import hashlib
import time

import discord
from discord import app_commands
from discord.ext import tasks

from analytics import record
from db import community
from features.humor_common import QUESTS, interaction_language, safe_text, text
from llm.humor import generate_humor


STRINGS = {
    "human": ("Only server members who are not bots can use these controls.", "Doar membrii serverului care nu sunt boți pot folosi aceste butoane."),
    "invalid_tribunal": ("Choose a human member and an accusation of 1–500 characters.", "Alege un membru uman și o acuzație de 1–500 de caractere."),
    "invalid_meeting": ("Provide a subject of 1–200 characters and 2–5 distinct options, separated by |, each at most 80 characters.", "Scrie un subiect de 1–200 de caractere și 2–5 opțiuni distincte, separate prin |, fiecare de cel mult 80 de caractere."),
    "tribunal": ("⚖️ Tribunal · member {member}\n{content}", "⚖️ Tribunal · membrul {member}\n{content}"),
    "invitation": ("This is a joke. Only the accused can accept or decline. Voting opens after acceptance and lasts exactly 10 minutes. Invitation expires: {deadline}.", "Este o glumă. Doar acuzatul poate accepta sau refuza. Votul începe după acceptare și durează exact 10 minute. Invitația expiră: {deadline}."),
    "meeting": ("📝 Meeting: {content}", "📝 Ședință: {content}"),
    "quest": ("🎯 Quest · member {member}\n{content}", "🎯 Quest · membrul {member}\n{content}"),
    "offer": ("Accept to publish this challenge here. You then have seven days to submit completion. Offer expires: {deadline}.", "Acceptă pentru a publica provocarea aici. Ai apoi șapte zile să anunți finalizarea. Oferta expiră: {deadline}."),
    "quest_active": ("Only the owner can submit completion or abandon. Completion deadline: {deadline}. Community verification then lasts 10 minutes.", "Doar titularul poate anunța finalizarea sau abandona. Termen de finalizare: {deadline}. Verificarea comunității durează apoi 10 minute."),
    "vote": ("One changeable vote per human member. Voting closes: {deadline}.", "Un vot modificabil pentru fiecare membru uman. Votul se închide: {deadline}."),
    "verification": ("The quest owner cannot verify their own completion. A strict positive majority grants a symbolic title, with no Discord role.", "Titularul questului nu își poate verifica propria finalizare. O majoritate strict pozitivă acordă un titlu simbolic, fără rol Discord."),
    "quiz": ("🏛️ Who said this?\n{content}", "🏛️ Cine a zis?\n{content}"),
    "correct": ("Correct author: {name}", "Autorul corect: {name}"),
    "winner": ("Result: {option}", "Rezultat: {option}"),
    "title": ("Symbolic title earned: {title}", "Titlu simbolic câștigat: {title}"),
    "no_title": ("No symbolic title: verification requires more Success votes than Not yet votes.", "Fără titlu simbolic: verificarea cere mai multe voturi Reușit decât Încă nu."),
    "closed": ("This session is closed.", "Această sesiune este închisă."),
    "cancelled": ("This session was cancelled.", "Această sesiune a fost anulată."),
    "declined": ("The invitation was declined.", "Invitația a fost refuzată."),
    "expired": ("The invitation or completion window expired.", "Invitația sau termenul de finalizare a expirat."),
    "quote_removed": ("This quote has been withdrawn. The quiz is unavailable.", "Citatul a fost retras. Quizul nu mai este disponibil."),
    "published": ("Published in this channel.", "Am publicat în acest canal."),
    "titles": ("Your symbolic titles: {titles}", "Titlurile tale simbolice: {titles}"),
    "another": ("Take another quest", "Ia alt quest"),
    "tribunal_fallback": ("The imaginary court has filed its paperwork beside the biscuit tin.", "Instanța imaginară și-a pus hârtiile lângă cutia cu biscuiți."),
    "sedinta_fallback": ("The minutes are ready; the stationery department requests a coffee break.", "Procesul-verbal este gata; departamentul de papetărie cere o pauză de cafea."),
}


def _t(key, language, **values):
    return STRINGS[key][language == "ro"].format(**values)


def _options(session):
    kind, payload, language = session["kind"], session["payload"], session["language"]
    if kind == "tribunal":
        return [(key, text(key, language)) for key in ("guilty", "innocent")]
    if kind == "quest":
        return [(key, text(key, language)) for key in ("success", "not_yet")]
    if kind == "sedinta":
        return [(str(index), option) for index, option in enumerate(payload["options"])]
    return [(str(candidate["user_id"]), candidate["name"]) for candidate in payload.get("candidates", [])]


def _winner(tally):
    maximum = max(tally.values(), default=0)
    winners = [key for key, count in tally.items() if count == maximum]
    return winners[0] if maximum > 0 and len(winners) == 1 else None


class SessionView(discord.ui.View):
    def __init__(self, feature, session, disabled=False):
        super().__init__(timeout=None)
        phase, kind, language = session["phase"], session["kind"], session["language"]
        if phase == "pending":
            actions = [(key, text(key, language)) for key in ("accept", "decline")]
        elif kind == "quest" and phase == "active":
            actions = [(key, text(key, language)) for key in ("completed", "abandon")]
        else:
            actions = _options(session)
        for action, label in actions:
            button = discord.ui.Button(label=str(label)[:80], custom_id=f"social:{session['id']}:{action}", disabled=disabled or phase not in community.ACTIVE_PHASES)

            async def callback(interaction, selected=action):
                await feature.handle_control(interaction, session["guild_id"], session["id"], selected)

            button.callback = callback
            self.add_item(button)


class QuestAgainView(discord.ui.View):
    def __init__(self, feature, owner_id, language):
        super().__init__(timeout=300)
        button = discord.ui.Button(label=_t("another", language))

        async def callback(interaction):
            if interaction.user.id != owner_id:
                await interaction.response.send_message(text("not_owner", await interaction_language(interaction)), ephemeral=True, allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
                return
            await feature.quest(interaction, offer_new=True)

        button.callback = callback
        self.add_item(button)


class SocialGamesFeature:
    def __init__(self, client, tree):
        self.client = client
        self.tree = tree
        self._locks = {}
        self._restored = set()
        self._startup_lock = None
        self._delivery_attempts = {}
        self._register_commands()

    def _lock(self, session_id):
        if session_id not in self._locks:
            self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]

    async def _record(self, category, activity, guild_id):
        await record(category, activity, guild_id=guild_id, scope_type="guild")

    async def _reply(self, interaction, content, view=None):
        kwargs = {"ephemeral": True, "allowed_mentions": discord.AllowedMentions.none(), "suppress_embeds": True}
        if view is not None:
            kwargs["view"] = view
        await interaction.followup.send(content[:2000], **kwargs)

    async def _defer(self, interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

    def _human(self, interaction):
        return interaction.guild_id is not None and not getattr(interaction.user, "bot", True) and getattr(interaction.guild, "id", None) == interaction.guild_id and getattr(getattr(interaction.user, "guild", None), "id", None) == interaction.guild_id

    async def _admit(self, interaction, language):
        if interaction.guild_id is None:
            await self._reply(interaction, text("guild_only", language))
            return False
        if not self._human(interaction) or getattr(interaction.channel, "id", None) != interaction.channel_id or getattr(getattr(interaction.channel, "guild", None), "id", None) != interaction.guild_id:
            await self._reply(interaction, _t("human", language))
            return False
        return True

    async def tribunal(self, interaction, accused, accusation):
        await self._defer(interaction)
        language = await interaction_language(interaction)
        if not await self._admit(interaction, language):
            return None
        if getattr(accused, "bot", True) or getattr(getattr(accused, "guild", None), "id", None) != interaction.guild_id or not isinstance(accusation, str) or not 1 <= len(accusation.strip()) <= 500:
            await self._reply(interaction, _t("invalid_tribunal", language))
            return None
        return await self._create_public(interaction, "tribunal", language, {"accusation": accusation.strip()}, subject_id=accused.id)

    async def meeting(self, interaction, subject, options):
        await self._defer(interaction)
        language = await interaction_language(interaction)
        if not await self._admit(interaction, language):
            return None
        choices = [part.strip() for part in options.split("|")] if isinstance(options, str) else []
        if not isinstance(subject, str) or not 1 <= len(subject.strip()) <= 200 or not 2 <= len(choices) <= 5 or any(not 1 <= len(option) <= 80 for option in choices) or len({option.casefold() for option in choices}) != len(choices):
            await self._reply(interaction, _t("invalid_meeting", language))
            return None
        return await self._create_public(interaction, "sedinta", language, {"subject": subject.strip(), "options": choices})

    async def quest(self, interaction, offer_new=False):
        await self._defer(interaction)
        language = await interaction_language(interaction)
        if not await self._admit(interaction, language):
            return None
        try:
            current = None
            after_id = 0
            while True:
                page = await asyncio.to_thread(community.list_restorable_sessions, interaction.guild_id, after_id=after_id)
                current = next((row for row in page if row["kind"] == "quest" and row["creator_id"] == interaction.user.id and row["phase"] in community.ACTIVE_PHASES + ("closing",)), current)
                if current or len(page) < 100:
                    break
                after_id = page[-1]["id"]
            if current:
                if current["deadline"] <= time.time():
                    await self._finish_session(current)
                    current = await asyncio.to_thread(community.get_session, interaction.guild_id, current["id"])
                if current["phase"] in community.ACTIVE_PHASES + ("closing",):
                    await self._reply(interaction, await self._render_session(current), SessionView(self, current) if current["phase"] == "pending" else None)
                    return current
            titles = await asyncio.to_thread(community.list_titles, interaction.guild_id, interaction.user.id)
            if titles and not offer_new:
                catalog = {quest["title_key"]: quest["title_" + language] for quest in QUESTS}
                names = ", ".join(safe_text(catalog.get(row["title_key"], row["title_key"]), 100) for row in titles)
                await self._reply(interaction, _t("titles", language, titles=names)[:1900], QuestAgainView(self, interaction.user.id, language))
                return None
            seed = hashlib.sha256(f"{interaction.guild_id}:{interaction.user.id}:{time.time_ns()}".encode()).digest()
            chosen = QUESTS[int.from_bytes(seed[:8], "big") % len(QUESTS)]
            payload = {"challenge_en": chosen["text_en"], "challenge_ro": chosen["text_ro"], "title_key": chosen["title_key"], "title_en": chosen["title_en"], "title_ro": chosen["title_ro"]}
            session = await asyncio.to_thread(community.create_session, interaction.guild_id, interaction.channel_id, interaction.user.id, "quest", language, payload)
            await self._reply(interaction, await self._render_session(session), SessionView(self, session) if session["phase"] == "pending" else None)
            return session
        except Exception:
            await self._record("failure", "community/quest", interaction.guild_id)
            await self._reply(interaction, text("failed", language))
            return None

    async def create_quiz(self, interaction, quote, candidates):
        await self._defer(interaction)
        language = await interaction_language(interaction)
        if not await self._admit(interaction, language):
            return None
        try:
            approved = await asyncio.to_thread(community.get_quote, interaction.guild_id, quote["id"], approved_only=True)
            if not approved or approved["guild_id"] != quote.get("guild_id", interaction.guild_id):
                await self._reply(interaction, _t("quote_removed", language))
                return None
            payload = {"quote_id": approved["id"], "quote_text": approved["quote_text"], "candidates": candidates, "correct_author_id": approved["author_id"]}
            return await self._create_public(interaction, "quiz", language, payload)
        except Exception:
            await self._record("failure", "community/quiz", interaction.guild_id)
            await self._reply(interaction, text("failed", language))
            return None

    async def _create_public(self, interaction, kind, language, payload, subject_id=None):
        session = None
        message = None
        try:
            session = await asyncio.to_thread(community.create_session, interaction.guild_id, interaction.channel_id, interaction.user.id, kind, language, payload, subject_id=subject_id)
            content = await self._render_session(session)
            mentions = discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(id=subject_id)], replied_user=False) if kind == "tribunal" else discord.AllowedMentions.none()
            message = await interaction.channel.send(content, view=SessionView(self, session), allowed_mentions=mentions, suppress_embeds=True)
            bound = await asyncio.to_thread(community.bind_session_message, interaction.guild_id, session["id"], message.id)
            if bound["status"] != "bound":
                raise ValueError("bind_failed")
            session = bound["session"]
            if kind == "quiz":
                approved = await asyncio.to_thread(community.get_quote, interaction.guild_id, payload["quote_id"], approved_only=True)
                if not approved:
                    await self._deliver_session(await asyncio.to_thread(community.get_session, interaction.guild_id, session["id"]))
                    await self._reply(interaction, _t("quote_removed", language))
                    return None
            await self._reply(interaction, _t("published", language))
            return session
        except Exception:
            if session:
                try:
                    cancelled = await asyncio.to_thread(community.cancel_session, interaction.guild_id, session["id"])
                    if message:
                        stopped = cancelled["session"] or dict(session, phase="cancelled")
                        await message.edit(content=_t("cancelled", language), view=SessionView(self, stopped, disabled=True), allowed_mentions=discord.AllowedMentions.none(), suppress=True)
                except Exception:
                    pass
            await self._record("failure", "community/create", interaction.guild_id)
            await self._reply(interaction, text("failed", language))
            return None

    async def handle_control(self, interaction, guild_id, session_id, action):
        await self._defer(interaction)
        language = await interaction_language(interaction)
        if not await self._admit(interaction, language):
            return
        if interaction.guild_id != guild_id:
            await self._reply(interaction, text("expired", language))
            return
        published = None
        try:
            async with self._lock(session_id):
                session = await asyncio.to_thread(community.get_session, guild_id, session_id)
                if not session or session["channel_id"] != interaction.channel_id or session["deadline"] <= time.time() or session["phase"] not in community.ACTIVE_PHASES:
                    await self._reply(interaction, text("expired", language))
                    return
                private_offer = session["kind"] == "quest" and session["phase"] == "pending"
                if not private_offer and getattr(interaction.message, "id", None) != session["message_id"]:
                    await self._reply(interaction, text("expired", language))
                    return
                if session["phase"] == "pending" or (session["kind"] == "quest" and session["phase"] == "active"):
                    owner = session["subject_id"] if session["kind"] == "tribunal" else session["creator_id"]
                    if interaction.user.id != owner:
                        await self._reply(interaction, text("not_owner", language))
                        return
                    allowed = {"accept": "active", "decline": "declined"} if session["phase"] == "pending" else {"completed": "verification", "abandon": "cancelled"}
                    if action not in allowed:
                        await self._reply(interaction, text("expired", language))
                        return
                    if private_offer and action == "accept":
                        member = interaction.guild.get_member(interaction.user.id)
                        if member is None:
                            member = await interaction.guild.fetch_member(interaction.user.id)
                        if member.bot or member.guild.id != guild_id:
                            await self._reply(interaction, _t("human", language))
                            return
                        if session["message_id"] is None:
                            provisional = dict(session, phase="active", deadline=time.time() + 604800)
                            published = await interaction.channel.send(await self._render_session(provisional, refresh=False), view=SessionView(self, provisional, disabled=True), allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
                            bound = await asyncio.to_thread(community.bind_session_message, guild_id, session_id, published.id)
                            if bound["status"] != "bound":
                                raise ValueError("bind_failed")
                    outcome = await asyncio.to_thread(community.transition_session, guild_id, session_id, session["phase"], allowed[action], interaction.user.id)
                    if outcome["status"] == "changed":
                        await self._record("control", "community/" + action, guild_id)
                        await self._reply(interaction, text("saved", language))
                        if outcome["session"]["message_id"]:
                            await self._deliver_session(outcome["session"], automatic=False)
                        return
                else:
                    if session["kind"] == "quest" and interaction.user.id == session["creator_id"]:
                        await self._reply(interaction, text("not_owner", language))
                        return
                    outcome = await asyncio.to_thread(community.cast_vote, guild_id, session_id, interaction.user.id, action)
                    if outcome["status"] == "changed":
                        await self._record("control", "community/vote", guild_id)
                    if outcome["status"] in ("changed", "unchanged"):
                        await self._reply(interaction, text("saved", language))
                        return
                await self._reply(interaction, text("expired", language))
        except Exception:
            if action == "accept":
                try:
                    current = await asyncio.to_thread(community.get_session, guild_id, session_id)
                    if current and current["kind"] == "quest" and current["phase"] == "pending":
                        cancelled = await asyncio.to_thread(community.cancel_session, guild_id, session_id)
                        if published:
                            await published.edit(content=_t("cancelled", current["language"]), view=SessionView(self, cancelled["session"], disabled=True), allowed_mentions=discord.AllowedMentions.none(), suppress=True)
                except Exception:
                    pass
            await self._record("failure", "community/control", guild_id)
            await self._reply(interaction, text("failed", language))

    async def _render_session(self, session, refresh=True):
        if refresh:
            current = await asyncio.to_thread(community.get_session, session["guild_id"], session["id"])
            if current:
                session = current
        language, phase, payload, kind = (session[key] for key in ("language", "phase", "payload", "kind"))
        if kind == "quiz":
            quote = await asyncio.to_thread(community.get_quote, session["guild_id"], payload.get("quote_id", 1), approved_only=True) if payload else None
            if not quote:
                return _t("quote_removed", language)
        if phase in ("cancelled", "declined", "expired"):
            return _t(phase, language)
        deadline = f"<t:{int(session['deadline'])}:F>"
        if kind == "tribunal":
            lines = [_t("tribunal", language, member=f"<@{session['subject_id']}>", content=safe_text(payload["accusation"], 650))]
        elif kind == "sedinta":
            lines = [_t("meeting", language, content=safe_text(payload["subject"], 260))]
        elif kind == "quest":
            lines = [_t("quest", language, member=f"<@{session['creator_id']}>", content=safe_text(payload["challenge_" + language], 350))]
        else:
            lines = [_t("quiz", language, content=safe_text(payload["quote_text"], 1250))]
        if phase == "pending":
            lines.append(_t("invitation" if kind == "tribunal" else "offer", language, deadline=deadline))
        elif phase == "active" and kind == "quest":
            lines.append(_t("quest_active", language, deadline=deadline))
        elif phase in ("active", "verification"):
            lines.append(_t("vote", language, deadline=deadline))
            if kind == "quest":
                lines.append(_t("verification", language))
            lines.extend(f"{index + 1}. {safe_text(label, 90)}" for index, (_, label) in enumerate(_options(session)))
        else:
            tally = await asyncio.to_thread(community.session_tally, session["guild_id"], session["id"]) or {}
            options = dict(_options(session))
            if kind == "quiz":
                lines.append(_t("correct", language, name=safe_text(options.get(str(payload["correct_author_id"]), "—"), 90)))
            elif kind == "quest":
                lines.append(_t("title", language, title=safe_text(payload["title_" + language], 100)) if tally.get("success", 0) > tally.get("not_yet", 0) else _t("no_title", language))
            else:
                winner = _winner(tally)
                lines.append(_t("winner", language, option=safe_text(options[winner], 90)) if winner else text("neutral", language))
            lines.extend(f"{safe_text(label, 90)}: {tally.get(key, 0)}" for key, label in options.items())
            if session["result_text"]:
                lines.append(session["result_text"][:400])
        return "\n".join(lines)[:2000]

    async def _finish_session(self, session):
        async with self._lock(session["id"]):
            session = await asyncio.to_thread(community.get_session, session["guild_id"], session["id"])
            if not session:
                return
            if session["phase"] in community.ACTIVE_PHASES:
                fallback = _t(session["kind"] + "_fallback", session["language"]) if session["kind"] in ("tribunal", "sedinta") and session["phase"] == "active" else _t("closed", session["language"])
                claimed = await asyncio.to_thread(community.claim_session_close, session["guild_id"], session["id"], fallback)
                if claimed["status"] not in ("claimed", "closed"):
                    return
                session = claimed["session"]
                if claimed["status"] == "claimed" and session["phase"] == "closing":
                    result = fallback
                    if session["kind"] in ("tribunal", "sedinta"):
                        try:
                            result = await generate_humor(session["kind"], session["language"], {"payload": session["payload"], "counts": claimed["tally"], "outcome": _winner(claimed["tally"]) or "neutral"}, fallback, background=True)
                            if not isinstance(result, str) or not result.strip() or len(result) > 400:
                                result = fallback
                        except Exception:
                            await self._record("failure", "community/generation", session["guild_id"])
                    saved = await asyncio.to_thread(community.save_session_result, session["guild_id"], session["id"], result)
                    session = saved["session"]
            elif session["phase"] == "closing":
                saved = await asyncio.to_thread(community.save_session_result, session["guild_id"], session["id"], session["result_text"] or _t("closed", session["language"]))
                session = saved["session"]
            if session:
                await self._deliver_session(session)

    async def _deliver_session(self, session, automatic=True):
        guild_id, session_id = session["guild_id"], session["id"]
        session = await asyncio.to_thread(community.get_session, guild_id, session_id)
        if not session or session["delivery_status"] == "error":
            return
        if session["message_id"] is None:
            if session["phase"] in community.TERMINAL_PHASES:
                await asyncio.to_thread(community.mark_session_delivery, guild_id, session_id, "delivered")
            return
        try:
            channel = self.client.get_channel(session["channel_id"])
            if channel is None:
                channel = await self.client.fetch_channel(session["channel_id"])
            if getattr(getattr(channel, "guild", None), "id", None) != guild_id:
                await asyncio.to_thread(community.mark_session_delivery, guild_id, session_id, "error", error_code="forbidden")
                await self._record("failure", "community/delivery", guild_id)
                return
            message = await channel.fetch_message(session["message_id"])
            for attempt in range(2):
                session = await asyncio.to_thread(community.get_session, guild_id, session_id)
                content = await self._render_session(session, refresh=False)
                await message.edit(content=content, view=SessionView(self, session), allowed_mentions=discord.AllowedMentions.none(), suppress=True)
                marked = await asyncio.to_thread(community.mark_session_delivery, guild_id, session_id, "delivered", expected_phase=session["phase"], expected_payload=session["payload"])
                if marked["status"] != "stale":
                    break
            else:
                return
            self._delivery_attempts.pop(session_id, None)
            if automatic:
                await self._record("scheduled", "community/result", guild_id)
        except (discord.NotFound, discord.Forbidden) as exc:
            await asyncio.to_thread(community.mark_session_delivery, guild_id, session_id, "error", error_code="not_found" if isinstance(exc, discord.NotFound) else "forbidden", expected_phase=session["phase"], expected_payload=session["payload"])
            await self._record("failure", "community/delivery", guild_id)
        except Exception:
            attempts = self._delivery_attempts.get(session_id, 0) + 1
            self._delivery_attempts[session_id] = attempts
            await asyncio.to_thread(community.mark_session_delivery, guild_id, session_id, "retry", retry_at=time.time() + min(300, 15 * 2 ** min(attempts - 1, 5)), error_code="http", expected_phase=session["phase"], expected_payload=session["payload"])
            await self._record("failure", "community/delivery", guild_id)

    async def start_tasks(self):
        if self._startup_lock is None:
            self._startup_lock = asyncio.Lock()
        async with self._startup_lock:
            after_id = 0
            try:
                while True:
                    page = await asyncio.to_thread(community.list_restorable_sessions, after_id=after_id)
                    for session in page:
                        key = (session["id"], session["message_id"], session["phase"])
                        if session["message_id"] and key not in self._restored:
                            try:
                                self.client.add_view(SessionView(self, session), message_id=session["message_id"])
                                self._restored.add(key)
                            except Exception:
                                await self._record("failure", "community/restore", session["guild_id"])
                    if len(page) < 100:
                        break
                    after_id = page[-1]["id"]
            except Exception:
                await self._record("failure", "community/restore", None)
            await self._tick()
            if not self._check.is_running():
                self._check.start()

    async def _tick(self):
        after_id = 0
        try:
            while True:
                page = await asyncio.to_thread(community.list_due_sessions, after_id=after_id)
                for session in page:
                    try:
                        await self._finish_session(session)
                    except Exception:
                        await self._record("failure", "community/tick", session["guild_id"])
                if len(page) < 100:
                    break
                after_id = page[-1]["id"]
            after_id = 0
            while True:
                page = await asyncio.to_thread(community.list_restorable_sessions, after_id=after_id)
                for session in page:
                    if session["phase"] in community.ACTIVE_PHASES and session["delivery_status"] == "retry" and (session["retry_at"] or 0) <= time.time():
                        try:
                            async with self._lock(session["id"]):
                                await self._deliver_session(session, automatic=False)
                        except Exception:
                            await self._record("failure", "community/tick", session["guild_id"])
                if len(page) < 100:
                    break
                after_id = page[-1]["id"]
            await asyncio.to_thread(community.cleanup_community)
        except Exception:
            await self._record("failure", "community/tick", None)

    @tasks.loop(seconds=15)
    async def _check(self):
        await self._tick()

    def _register_commands(self):
        @self.tree.command(name="tribunal", description="Tribunal: toți membrii votează imediat / All members vote immediately")
        @app_commands.guild_only()
        @app_commands.describe(acuzat="Membrul acuzat / Accused member", acuzatie="Acuzație în glumă / Joke accusation, maximum 500 characters")
        async def tribunal(interaction: discord.Interaction, acuzat: discord.Member, acuzatie: str):
            await self.tribunal(interaction, acuzat, acuzatie)

        @self.tree.command(name="sedinta", description="Vot de zece minute / Ten-minute meeting vote")
        @app_commands.guild_only()
        @app_commands.describe(subiect="Subiectul / Subject", optiuni="2–5 opțiuni separate prin | / 2–5 options separated by |")
        async def sedinta(interaction: discord.Interaction, subiect: str, optiuni: str):
            await self.meeting(interaction, subiect, optiuni)

        @self.tree.command(name="quest", description="Provocare sigură și titluri simbolice / Safe challenge and symbolic titles")
        @app_commands.guild_only()
        async def quest(interaction: discord.Interaction):
            await self.quest(interaction)
