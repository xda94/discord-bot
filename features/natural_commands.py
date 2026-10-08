"""Mention-only natural commands with immediate, privacy-preserving delivery."""

from __future__ import annotations

import time
from dataclasses import replace

import db
from db import reminders as reminder_store
from db.connection import _connect
from analytics import record_for, record, guild_id_from
from discord.ext import tasks
from assistant_profiles import effective_profile
from command_time import calendar_time, reminder_time, duration, CURRENCY_ALIASES, number
from i18n import language_for, localize, fold, t
from natural_commands import CalendarReminderAction, ReminderManageAction, NaturalClarification
from action_proposals import validate_proposal, authorize_proposal

import discord

from features.flights import format_user_flight_trackers
from mention_utils import extract_mention_text
from natural_commands import (
    ClearWishlistTargetAction,
    DeleteWishlistAction,
    NaturalCommandError,
    RefreshWishlistAction,
    ReminderAction,
    ShowFlightsAction,
    ShowWishlistAction,
    TrackURLAction,
    WishlistGraphAction,
    WishlistRestockAction,
    WishlistTargetAction,
    parse_natural_command,
    suggest_natural_command,
)


class NaturalCommandSuggestionView(discord.ui.View):
    def __init__(self, feature, message, canonical, replied_text, language):
        super().__init__(timeout=600)
        self.feature = feature
        self.message = message
        self.canonical = canonical
        self.replied_text = replied_text
        self.language = language
        self.deadline = time.monotonic() + 600
        self.consumed = False
        yes = discord.ui.Button(label=t('natural_suggestion_yes', language), style=discord.ButtonStyle.primary)
        no = discord.ui.Button(label=t('natural_suggestion_no', language), style=discord.ButtonStyle.secondary)
        yes.callback = self.confirm
        no.callback = self.cancel
        self.add_item(yes)
        self.add_item(no)

    async def _consume(self, interaction):
        if interaction.user.id != self.message.author.id:
            await interaction.response.send_message(t('not_owner', self.language), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return False
        if self.consumed or time.monotonic() >= self.deadline:
            await interaction.response.send_message(t('expired', self.language), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return False
        self.consumed = True
        self.stop()
        self.clear_items()
        return True

    async def confirm(self, interaction):
        if not await self._consume(interaction):
            return
        await interaction.response.edit_message(view=None)
        parsed = parse_natural_command(self.canonical, replied_text=self.replied_text)
        if parsed is None or isinstance(parsed, NaturalCommandError):
            await self.feature._reply_error(self.message, t('invalid', self.language))
            return
        await self.feature.execute_parsed(self.message, parsed, language=self.language)

    async def cancel(self, interaction):
        if not await self._consume(interaction):
            return
        await interaction.response.edit_message(content=t('natural_suggestion_cancelled', self.language), view=None, allowed_mentions=discord.AllowedMentions.none())
        await self.feature._outcome(self.message, 'unknown', self.language, 'parser', 'abandoned')

    async def on_timeout(self):
        self.consumed = True
        self.stop()
        self.clear_items()


class NaturalCommandsFeature:
    def __init__(self, client, *, bot_id: int, wishlist, flights, reminders):
        self.client = client
        self.bot_id = bot_id
        self.wishlist = wishlist
        self.flights = flights
        self.reminders = reminders
        self.pending = {}
        self.recent = {}

    async def start_tasks(self):
        if not self._expire.is_running():
            self._expire.start()

    @tasks.loop(seconds=30)
    async def _expire(self):
        for key, recent in list(self.recent.items()):
            if recent[0] < time.monotonic():
                self.recent.pop(key,None)
        for key, waiting in list(self.pending.items()):
            if waiting[0] > time.monotonic():
                continue
            self.pending.pop(key, None)
            lang, guild_id, route = waiting[3:] if len(waiting) > 3 else ('en', None, 'parser')
            await record('control', f'natural/{waiting[1].intent}/{lang}/{route}/timeout', guild_id=guild_id, scope_type='guild' if guild_id is not None else 'dm')

    async def _acknowledge(self, message) -> None:
        """Best-effort success reaction; never rerun the durable action."""
        try:
            await message.add_reaction("✅")
        except Exception:
            pass

    async def _reply_error(self, message, text: str) -> None:
        return await message.reply(
            localize(text, language_for(extract_mention_text(message, self.bot_id) or "", db.get_assistant_profile(message.author.id))),
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
            suppress_embeds=True,
        )

    async def _send_private(self, message, chunks) -> bool:
        """Send all result chunks to the author, never to the source channel."""
        delivered = True
        for chunk in chunks:
            try:
                await message.author.send(
                    chunk,
                    allowed_mentions=discord.AllowedMentions.none(),
                    suppress_embeds=True,
                )
            except Exception:
                delivered = False
        return delivered

    async def _track(self, user_id: int, url: str, language: str):
        """Return the add status without exposing scraped item details."""
        result = await self.wishlist.add_item_for_user(user_id, url, language=language)
        return result.status

    async def _suggest_or_fall_through(self, message, text, replied_text, language):
        canonical = suggest_natural_command(text, replied_text=replied_text)
        if canonical is None:
            return False
        prompt = t('natural_suggestion', language, command=canonical)
        kwargs = {}
        if len(prompt.encode('utf-16-le')) // 2 > 2000:
            prompt = t('natural_suggestion_long', language)
        else:
            kwargs['view'] = NaturalCommandSuggestionView(self, message, canonical, replied_text, language)
        await message.reply(prompt, mention_author=False, allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True, **kwargs)
        await self._outcome(message, 'unknown', language, 'parser', 'clarification')
        return True

    async def handle_message(self, message: discord.Message) -> bool:
        text = extract_mention_text(message, self.bot_id)
        if text is None:
            return False
        replied_text = ""
        resolved = getattr(getattr(message, "reference", None), "resolved", None)
        if resolved is not None:
            replied_text = (
                getattr(resolved, "clean_content", None)
                or getattr(resolved, "content", "")
            )
        profile = effective_profile(db.get_assistant_profile(message.author.id))
        lang = language_for(text, profile)
        pending_key = (message.author.id, message.channel.id)
        waiting = self.pending.get(pending_key)
        if waiting and waiting[0] < time.monotonic():
            self.pending.pop(pending_key, None)
            await self._outcome(message, waiting[1].intent, waiting[3], waiting[5], 'timeout')
            waiting = None
        if waiting and fold(text) in ('cancel','anuleaza','renunta'):
            self.pending.pop(pending_key, None)
            await self._outcome(message,waiting[1].intent,lang,waiting[5] if len(waiting)>5 else 'parser','abandoned')
            await self._acknowledge(message)
            return True
        parsed = parse_natural_command(text, replied_text=replied_text)
        if waiting and parsed is None:
            lang = language_for(text,profile,fallback=waiting[3] if len(waiting)>3 else lang)
            request = waiting[1]
            slots = dict(request.slots)
            if request.field == 'timezone':
                from assistant_profiles import validate_timezone
                try:
                    zone = validate_timezone(text)
                except ValueError:
                    return await self._suggest_or_fall_through(message, text, replied_text, lang)
                if db.set_assistant_profile(message.author.id, timezone=zone) is None:
                    await self._reply_error(message,t('failed',lang))
                    return True
                profile = effective_profile(db.get_assistant_profile(message.author.id))
            elif request.field == 'currency':
                currency = CURRENCY_ALIASES.get(fold(text).strip())
                if currency is None:
                    return await self._suggest_or_fall_through(message, text, replied_text, lang)
                slots['currency'] = currency
            elif request.field == 'price':
                words = text.split()
                if not 1 <= len(words) <= 2:
                    return await self._suggest_or_fall_through(message, text, replied_text, lang)
                if len(words) == 2 and fold(words[1]) not in CURRENCY_ALIASES:
                    return await self._suggest_or_fall_through(message, text, replied_text, lang)
                price = number(words[0],lang) if words else None
                if price is None or price<=0:
                    return await self._suggest_or_fall_through(message, text, replied_text, lang)
                slots['price']=price
                if len(words)==2:
                    slots['currency']=CURRENCY_ALIASES.get(fold(words[1]))
            elif request.field == 'reference':
                if len(text) > 300:
                    return await self._suggest_or_fall_through(message, text, replied_text, lang)
                if request.intent == 'track':
                    from wishlist.scraper import _is_valid_http_url
                    if not _is_valid_http_url(text):
                        return await self._suggest_or_fall_through(message, text, replied_text, lang)
                else:
                    with _connect() as c:
                        choices = c.execute('SELECT id,title FROM scraped_items WHERE user_id=?', (message.author.id,)).fetchall()
                    if not any(str(r[0]) == text.lstrip('#') or fold(text) in fold(r[1] or '') for r in choices):
                        return await self._suggest_or_fall_through(message, text, replied_text, lang)
                slots['reference'] = text
            elif request.field == 'when':
                if not (duration(text) or reminder_time(text,profile.timezone,recurrence=slots.get('recurrence'))):
                    return await self._suggest_or_fall_through(message, text, replied_text, lang)
                slots['when']=text
            elif request.field == 'text':
                reply_id = getattr(getattr(message, 'reference', None), 'message_id', None)
                explicit = text.startswith(('text:', 'mesaj:', 'message:'))
                if not explicit and (len(waiting) < 3 or not waiting[2] or reply_id != waiting[2]):
                    return await self._suggest_or_fall_through(message, text, replied_text, lang)
                slots['text'] = text.split(':', 1)[1].strip() if explicit else text
            self.pending.pop(pending_key,None)
            try:
                parsed=validate_proposal({'intent':request.intent,'slots':slots})
            except ValueError:
                parsed=NaturalCommandError(t('invalid',lang))
        if parsed is None:
            return await self._suggest_or_fall_through(message, text, replied_text, lang)
        return await self.execute_parsed(message, parsed, profile=profile, language=lang)

    async def _outcome(self,message,intent,language,route,outcome):
        await record_for('control',f'natural/{intent}/{language}/{route}/{outcome}',message)

    async def execute_proposal(self,message,proposal):
        lang=language_for(extract_mention_text(message,self.bot_id) or '',db.get_assistant_profile(message.author.id))
        try:
            parsed=validate_proposal(proposal)
            authorize_proposal(proposal,extract_mention_text(message,self.bot_id) or "",getattr(getattr(getattr(message,"reference",None),"resolved",None),"content",""))
        except (ValueError, TypeError, KeyError):
            await self._reply_error(message,t('invalid',lang))
            await self._outcome(message,'unknown',lang,'llm','invalid')
            return True
        return await self.execute_parsed(message,parsed,language=lang,route='llm')

    async def execute_parsed(self,message,parsed,*,profile=None,language=None,route='parser'):
        profile=profile or effective_profile(db.get_assistant_profile(message.author.id))
        lang=language or language_for(extract_mention_text(message,self.bot_id) or '',profile)
        intent = ({'TrackURLAction':'track','ShowWishlistAction':'wishlist','ShowFlightsAction':'flights','ReminderAction':'reminder','CalendarReminderAction':'reminder','DeleteWishlistAction':'delete_item','WishlistTargetAction':'target','ClearWishlistTargetAction':'clear_target','WishlistRestockAction':'restock','RefreshWishlistAction':'refresh','WishlistGraphAction':'graph'}.get(type(parsed).__name__, 'unknown'))
        if isinstance(parsed, ReminderManageAction):
            intent = 'reminder_' + parsed.operation
        if isinstance(parsed,NaturalCommandError):
            await self._reply_error(message,t(parsed.key,lang))
            await self._outcome(message,intent,lang,route,'invalid')
            return True
        if isinstance(parsed,NaturalClarification):
            previous = self.pending.get((message.author.id,message.channel.id))
            if previous:
                await self._outcome(message,previous[1].intent,lang,previous[5] if len(previous)>5 else 'parser','abandoned')
            prompt = await self._reply_error(message,t(parsed.key,lang))
            prompt_id = getattr(prompt, 'id', None)
            self.pending[(message.author.id,message.channel.id)] = (time.monotonic()+600, parsed, prompt_id if isinstance(prompt_id, int) else None, lang, guild_id_from(message), route)
            await self._outcome(message,parsed.intent,lang,route,'clarification')
            return True
        if isinstance(parsed,CalendarReminderAction):
            slots={'when':parsed.when,'text':parsed.text,'recurrence':parsed.recurrence}
            if not profile.timezone_configured:
                return await self.execute_parsed(message,NaturalClarification('timezone','reminder',slots,'timezone'),profile=profile,language=lang,route=route)
            at=reminder_time(parsed.when,profile.timezone,recurrence=parsed.recurrence)
            if not at:
                return await self.execute_parsed(message,NaturalClarification('when','reminder',slots,'time'),profile=profile,language=lang,route=route)
        if isinstance(parsed, ReminderManageAction) and parsed.operation == 'edit':
            slots = {'reminder_id': parsed.reminder_id, 'when': parsed.when, 'text': parsed.text}
            if not parsed.when and not parsed.text:
                return await self.execute_parsed(message, NaturalClarification('when', 'reminder_edit', slots, 'time'), profile=profile, language=lang, route=route)
            if parsed.when and not duration(parsed.when) and not profile.timezone_configured:
                return await self.execute_parsed(message, NaturalClarification('timezone', 'reminder_edit', slots, 'timezone'), profile=profile, language=lang, route=route)
            if parsed.when and not (duration(parsed.when) or calendar_time(parsed.when, profile.timezone)):
                return await self.execute_parsed(message, NaturalClarification('when', 'reminder_edit', slots, 'time'), profile=profile, language=lang, route=route)
        # Resolve personal references from IDs/titles, never from model-owned IDs.
        if isinstance(parsed,(DeleteWishlistAction,WishlistTargetAction,ClearWishlistTargetAction,WishlistRestockAction,RefreshWishlistAction,WishlistGraphAction)) and parsed.url and not parsed.url.startswith(('https://','http://')):
            with _connect() as c:
                rows=c.execute('SELECT id,url,title FROM scraped_items WHERE user_id=?',(message.author.id,)).fetchall()
            reference=fold(parsed.url).strip().lstrip('#')
            matches=[r for r in rows if str(r[0])==reference or fold(r[2] or '')==reference]
            if not matches:
                matches=[r for r in rows if reference in fold(r[2] or '')]
            if reference in ('asta','acesta','this','it'):
                recent=self.recent.get((message.author.id,message.channel.id))
                resolved=getattr(getattr(message,'reference',None),'resolved',None)
                reply_url = getattr(resolved,'content','') if resolved and getattr(getattr(resolved,'author',None),'id',None)==self.bot_id else ''
                matches=[r for r in rows if r[1] in reply_url] or ([r for r in rows if r[1]==recent[1]] if recent and recent[0]>time.monotonic() else [])
            if len(matches)==1:
                parsed=replace(parsed,url=matches[0][1])
            elif matches:
                slots = {'reference':parsed.url}
                for field in ('price','currency','days','percentage','enabled'):
                    if hasattr(parsed,field) and getattr(parsed,field) is not None:
                        slots[field] = getattr(parsed,field)
                pending_key = (message.author.id,message.channel.id)
                selection = (time.monotonic()+600,NaturalClarification('reference',intent,slots,'select'),None,lang,guild_id_from(message),route)
                self.pending[pending_key] = selection
                view=discord.ui.View(timeout=600)
                options=[discord.SelectOption(label=(r[2] or r[1])[:95],value=str(r[0])) for r in matches[:25]]
                select=discord.ui.Select(placeholder=t('select',lang),options=options)
                async def choose(interaction):
                    if interaction.user.id!=message.author.id:
                        await interaction.response.send_message(t('not_owner',lang),ephemeral=True)
                        return
                    if self.pending.get(pending_key) is not selection or selection[0] < time.monotonic():
                        await interaction.response.send_message(t('expired',lang),ephemeral=True)
                        return
                    self.pending.pop(pending_key,None)
                    await interaction.response.defer()
                    await self.execute_parsed(message,replace(parsed,url=next(r[1] for r in matches if str(r[0])==select.values[0])),profile=profile,language=lang,route=route)
                    view.stop()
                select.callback=choose
                view.add_item(select)
                try:
                    await message.author.send(t('select',lang),view=view)
                except discord.HTTPException:
                    await self._reply_error(message,t('private_failed',lang,command='/wishlist-show'))
                await self._outcome(message,intent,lang,route,'clarification')
                return True
            else:
                await self._reply_error(message,t('not_found',lang))
                await self._outcome(message,intent,lang,route,'invalid')
                return True
        event_id=getattr(message,'id',None)
        if event_id is not None and not reminder_store.claim_action(event_id):
            return True
        try:
            result=await self._execute(message,parsed,profile,lang)
            if event_id is not None:
                reminder_store.finish_action(event_id,bool(result))
            await self._outcome(message,intent,lang,route,'executed' if result else 'execution_failed')
            return True
        except Exception:
            if event_id is not None:
                reminder_store.finish_action(event_id,False)
            await self._reply_error(message,t('failed',lang))
            await self._outcome(message,intent,lang,route,'execution_failed')
            return True

    async def _execute(self,message,parsed,profile,lang):
        author_id = message.author.id
        try:
            if isinstance(parsed, ReminderManageAction):
                if parsed.operation=='list':
                    for text,view in self.reminders.format_for_user(author_id, language=lang):
                        await message.author.send(text,view=view or discord.utils.MISSING,allowed_mentions=discord.AllowedMentions.none())
                    await self._acknowledge(message)
                    return True
                if parsed.operation=='edit':
                    row=reminder_store.get(parsed.reminder_id,author_id)
                    when=time.time()+duration(parsed.when) if parsed.when and duration(parsed.when) else calendar_time(parsed.when,profile.timezone) if parsed.when and profile.timezone_configured else None
                    updates = {}
                    if when:
                        updates['remind_at'] = when
                        if parsed.when and not duration(parsed.when):
                            updates['timezone'] = profile.timezone
                    if parsed.text:
                        updates['message'] = parsed.text
                    ok = bool(row and updates and reminder_store.edit(parsed.reminder_id,author_id,**updates))
                elif parsed.operation=='cancel':
                    ok=reminder_store.cancel(parsed.reminder_id,author_id)
                else:
                    ok=reminder_store.snooze(parsed.reminder_id,author_id)
                if ok:
                    await self._acknowledge(message)
                else:
                    await self._reply_error(message,t('not_found',lang))
                return bool(ok)
            if isinstance(parsed, CalendarReminderAction):
                at=reminder_time(parsed.when,profile.timezone,recurrence=parsed.recurrence)
                rid=self.reminders.create_reminder_at(author_id,message.channel.id,at,parsed.text,creator_id=author_id,timezone=profile.timezone,language=lang,recurrence=parsed.recurrence)
                if rid:
                    await self._acknowledge(message)
                else:
                    await self._reply_error(message,t('failed',lang))
                return bool(rid)
            if isinstance(parsed, ReminderAction):
                remind_at = time.time() + parsed.seconds
                reminder_id = self.reminders.create_reminder_at(
                    author_id, message.channel.id, remind_at, parsed.text,
                    creator_id=author_id, timezone=profile.timezone, language=lang
                )
                if reminder_id is None:
                    await self._reply_error(
                        message, "I couldn't save that reminder. Please try again."
                    )
                    return False
                await self._acknowledge(message)
                return True

            if isinstance(parsed, TrackURLAction):
                status = await self._track(author_id, parsed.url, lang)
                if status == "added":
                    await self._acknowledge(message)
                else:
                    errors = {
                        "exists": "That URL is already in your tracking list.",
                        "blocked": "The source blocked or timed out, so the URL was not added.",
                        "busy": "Price/stock extraction is temporarily busy. Please try again shortly.",
                        "unsupported": "The page had no supported price or stock data, so the URL was not added.",
                        "database-error": "The page was read, but the database could not save it. Please try again.",
                        "invalid": "That is not a valid HTTP(S) URL.",
                    }
                    await self._reply_error(message, errors.get(status, "The URL could not be added."))
                return status == "added"

            if isinstance(parsed, ShowFlightsAction):
                delivered = await self._send_private(
                    message, format_user_flight_trackers(author_id, language=lang)
                )
                if delivered:
                    await self._acknowledge(message)
                else:
                    await self._reply_error(
                        message,
                        t("private_failed",lang,command="/flight-tracker-show" if isinstance(parsed,ShowFlightsAction) else "/wishlist-show"),
                    )
                return delivered

            if isinstance(parsed, ShowWishlistAction):
                chunks = self.wishlist.format_items_for_user(author_id, parsed.currency, language=lang)
                delivered = await self._send_private(message, chunks)
                if delivered:
                    from features.wishlist_controls import WishlistManageView
                    view=WishlistManageView(author_id, language=lang)
                    if view.children:
                        await message.author.send(t('select',lang),view=view)
                    from db.connection import _connect
                    with _connect() as c:
                        urls=c.execute('SELECT url FROM scraped_items WHERE user_id=?',(author_id,)).fetchall()
                    if len(urls)==1:
                        self.recent[(author_id,message.channel.id)]=(time.monotonic()+600,urls[0][0])
                if delivered:
                    await self._acknowledge(message)
                else:
                    await self._reply_error(
                        message,
                        t("private_failed",lang,command="/flight-tracker-show" if isinstance(parsed,ShowFlightsAction) else "/wishlist-show"),
                    )
                return delivered

            if isinstance(parsed, DeleteWishlistAction):
                if self.wishlist.delete_item_for_user(author_id, parsed.url):
                    await self._acknowledge(message)
                else:
                    await self._reply_error(message, "That wishlist change could not be applied.")
                    return False
                return True

            if isinstance(parsed, WishlistTargetAction):
                if self.wishlist.set_target_for_user(
                    author_id, parsed.url, parsed.price, parsed.currency
                ):
                    await self._acknowledge(message)
                else:
                    await self._reply_error(message, "That wishlist change could not be applied.")
                    return False
                return True

            if isinstance(parsed, ClearWishlistTargetAction):
                if self.wishlist.clear_target_for_user(author_id, parsed.url):
                    await self._acknowledge(message)
                else:
                    await self._reply_error(message, "That wishlist change could not be applied.")
                    return False
                return True

            if isinstance(parsed, WishlistRestockAction):
                if self.wishlist.set_restock_only_for_user(
                    author_id, parsed.url, parsed.enabled
                ):
                    await self._acknowledge(message)
                else:
                    await self._reply_error(message, "That wishlist change could not be applied.")
                    return False
                return True

            if isinstance(parsed, RefreshWishlistAction):
                chunks = await self.wishlist.refresh_items_for_user(author_id, parsed.url)
                protected = [value for row in db.get_user_scraped_items(author_id) for value in (row[0], f"**{row[3]}**" if row[3] else None)]
                chunks = [localize(chunk, lang, protected=protected) for chunk in chunks]
                delivered = await self._send_private(message, chunks)
                if delivered:
                    await self._acknowledge(message)
                else:
                    await self._reply_error(
                        message,
                        t("private_failed",lang,command="/wishlist-refresh"),
                    )
                return delivered

            if isinstance(parsed, WishlistGraphAction):
                await self.wishlist.send_graph_for_user(
                    message.author,
                    url=parsed.url,
                    currency=parsed.currency,
                    days=parsed.days,
                    percentage=parsed.percentage,
                    language=lang,
                )
                await self._acknowledge(message)
                return True

        except Exception:
            # A recognized command is consumed even when its helper or private
            # transport fails, so the ordinary LLM cannot duplicate the action.
            command = '/reminder-list' if isinstance(parsed,ReminderManageAction) and parsed.operation=='list' else '/wishlist-graph' if isinstance(parsed,WishlistGraphAction) and parsed.url else '/wishlist-graph-all' if isinstance(parsed,WishlistGraphAction) else None
            await self._reply_error(message,t('private_failed',lang,command=command) if command else t('failed',lang))
            return False

        return False
