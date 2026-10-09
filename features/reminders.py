from __future__ import annotations
import logging
import asyncio
import time
from typing import Optional
from datetime import datetime
from zoneinfo import ZoneInfo
import discord
from discord import app_commands
from discord.ext import tasks
import db
from db import reminders as store
from analytics import record_for
from assistant_profiles import effective_profile
from command_time import duration, calendar_time, reminder_time
from i18n import language_for, t

logger=logging.getLogger('discord_bot')


def _parse_time(value):
    return duration(value)


class ReminderEditModal(discord.ui.Modal):
    def __init__(self, reminder, language):
        super().__init__(title=t('edit',language))
        self.reminder_id=reminder['id']
        self.language=language
        self.when=discord.ui.TextInput(label='YYYY-MM-DD HH:MM',default=datetime.fromtimestamp(reminder['remind_at'],ZoneInfo(reminder['timezone'])).strftime('%Y-%m-%d %H:%M'))
        self.text=discord.ui.TextInput(label=t('message_label',language),default=reminder['message'],style=discord.TextStyle.long,max_length=1900)
        self.add_item(self.when)
        self.add_item(self.text)
    async def on_submit(self,interaction):
        if not effective_profile(db.get_assistant_profile(interaction.user.id)).timezone_configured:
            await interaction.response.send_message(t('timezone_required',self.language),ephemeral=True)
            return
        if not store.claim_action(f'modal:{interaction.id}'):
            await interaction.response.defer()
            return
        row=store.get(self.reminder_id,interaction.user.id)
        when=calendar_time(str(self.when),row['timezone']) if row else None
        try:
            ok=bool(when and store.edit(self.reminder_id,interaction.user.id,remind_at=when,message=str(self.text)))
        except ValueError:
            ok=False
        store.finish_action(f'modal:{interaction.id}', ok)
        await interaction.response.send_message(t('saved' if ok else 'time',self.language),ephemeral=True)


class ReminderControls(discord.ui.View):
    def __init__(self,reminder_id,language='en',*,delivered=False,failed=False):
        super().__init__(timeout=None)
        self.reminder_id=reminder_id
        self.language=language
        for operation in (('snooze',) if delivered else ('edit','cancel') + (('retry',) if failed else ())):
            button=discord.ui.Button(label=t(operation,language),custom_id=f'reminder:{reminder_id}:{operation}',style=discord.ButtonStyle.secondary)
            async def callback(interaction,op=operation):
                row=store.get(self.reminder_id,interaction.user.id)
                if not row:
                    await interaction.response.send_message(t('not_owner',self.language),ephemeral=True)
                    return
                if op=='edit':
                    if not effective_profile(db.get_assistant_profile(interaction.user.id)).timezone_configured:
                        await interaction.response.send_message(t('timezone_required',self.language),ephemeral=True)
                        return
                    await interaction.response.send_modal(ReminderEditModal(row,self.language))
                    return
                if not store.claim_action(f'button:{interaction.id}'):
                    await interaction.response.defer()
                    return
                ok=store.cancel(self.reminder_id,interaction.user.id) if op=='cancel' else store.retry(self.reminder_id,interaction.user.id) if op=='retry' else store.snooze(self.reminder_id,interaction.user.id)
                store.finish_action(f'button:{interaction.id}',bool(ok))
                await interaction.response.send_message(t('saved' if ok else 'failed',self.language),ephemeral=True)
            button.callback=callback
            self.add_item(button)


class RemindersFeature:
    def __init__(self,client,tree):
        self.client=client
        self.tree=tree
        self._register_commands()
    async def start_tasks(self):
        for row in await asyncio.to_thread(store.list_for):
            self.client.add_view(ReminderControls(row['id'],row['language'],failed=True))
            self.client.add_view(ReminderControls(row['id'],row['language'],delivered=True))
        if not self._check.is_running():
            self._check.start()
    @staticmethod
    def create_reminder_at(user_id,channel_id,remind_at,message,**options):
        try:
            return store.create(user_id,channel_id,remind_at,message,**options)
        except Exception:
            logger.exception('Could not save reminder')
            return None
    @staticmethod
    def format_for_user(user_id, *, language=None):
        profile=effective_profile(db.get_assistant_profile(user_id))
        lang=language or language_for(profile=profile)
        rows=store.list_for(user_id)
        if not rows:
            return [(t('empty_reminders',lang),None)]
        result=[]
        for row in rows:
            when=datetime.fromtimestamp(row['remind_at'],ZoneInfo(row['timezone'])).strftime('%Y-%m-%d %H:%M')
            state={'pending':'în așteptare','sending':'în curs','delivered':'trimis','cancelled':'anulat','failed':'eșuat'}.get(row['state'],row['state']) if lang=='ro' else row['state']
            recurrence = {'daily':'zilnic','weekdays':'zile lucrătoare','weekly':'săptămânal'}.get(row['recurrence'], row['recurrence'] or '—') if lang == 'ro' else row['recurrence'] or '—'
            text = f"**#{row['id']}** · {when} ({row['timezone']}) · {state}\n{row['message']}\n{recurrence}"
            if row['last_error']:
                key = 'delivery_permission' if row['last_error'] in ('Forbidden','NotFound') else 'delivery_uncertain' if row['last_error'] == 'uncertain-delivery' else 'delivery_error'
                text += "\n⚠️ " + t(key,lang,reason=row['last_error'])
            view = None if row['state']=='cancelled' else ReminderControls(row['id'],lang,delivered=row['state']=='delivered',failed=row['state']=='failed')
            parts = [text[i:i+1900] for i in range(0,len(text),1900)]
            result.extend((part, view if index == len(parts)-1 else None) for index,part in enumerate(parts))

        return result
    def _register_commands(self):
        @self.tree.command(name='remind',description='Set a reminder')
        @app_commands.describe(when='Duration or date and time',who='User to remind',what='Message',recurrence='daily, weekdays, or weekly')
        @app_commands.choices(recurrence=[app_commands.Choice(name=x,value=x) for x in ('daily','weekdays','weekly')])
        async def remind(interaction:discord.Interaction,when:str,who:discord.Member,what:str,recurrence:Optional[str]=None):
            p=effective_profile(db.get_assistant_profile(interaction.user.id))
            lang=language_for(what,p)
            seconds=duration(when)
            if (recurrence or seconds is None) and not p.timezone_configured:
                await interaction.response.send_message(t('timezone_required',lang),ephemeral=True)
                return
            at=time.time()+seconds if seconds else reminder_time(when,p.timezone,recurrence=recurrence)
            if not at:
                await interaction.response.send_message(t('time',lang),ephemeral=True)
                return
            rid=await asyncio.to_thread(self.create_reminder_at,who.id,interaction.channel_id,at,what,creator_id=interaction.user.id,timezone=p.timezone,language=lang,recurrence=recurrence)
            await interaction.response.send_message(t('saved' if rid else 'failed',lang),ephemeral=True)
        @self.tree.command(name='reminder-list',description='Show and manage your reminders privately')
        async def reminder_list(interaction:discord.Interaction):
            for index,(text,view) in enumerate(self.format_for_user(interaction.user.id)):
                send=interaction.response.send_message if index==0 else interaction.followup.send
                await send(text,view=view or discord.utils.MISSING,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
        @self.tree.command(name='reminder-edit',description='Edit your reminder')
        @app_commands.rename(reminder_id="reminder-id")
        async def reminder_edit(interaction:discord.Interaction,reminder_id:int):
            lang=language_for(profile=db.get_assistant_profile(interaction.user.id))
            row=store.get(reminder_id,interaction.user.id)
            if row:
                if not effective_profile(db.get_assistant_profile(interaction.user.id)).timezone_configured:
                    await interaction.response.send_message(t('timezone_required',lang),ephemeral=True)
                    return
                await interaction.response.send_modal(ReminderEditModal(row,lang))
            else:
                await interaction.response.send_message(t('not_found',lang),ephemeral=True)
        @self.tree.command(name='reminder-cancel',description='Cancel your reminder')
        @app_commands.rename(reminder_id="reminder-id")
        async def reminder_cancel(interaction:discord.Interaction,reminder_id:int):
            lang=language_for(profile=db.get_assistant_profile(interaction.user.id))
            await interaction.response.send_message(t('saved' if store.cancel(reminder_id,interaction.user.id) else 'not_found',lang),ephemeral=True)
        @self.tree.command(name='reminder-snooze',description='Snooze a reminder for ten minutes')
        @app_commands.rename(reminder_id="reminder-id")
        async def reminder_snooze(interaction:discord.Interaction,reminder_id:int):
            lang=language_for(profile=db.get_assistant_profile(interaction.user.id))
            await interaction.response.send_message(t('saved' if store.snooze(reminder_id,interaction.user.id) else 'not_found',lang),ephemeral=True)
    @tasks.loop(seconds=10)
    async def _check(self):
        try:
            # Reconcile uncertain sends using an occurrence marker in the embed.
            for row in await asyncio.to_thread(store.list_for):
                if row['state']=='failed' and row['last_error']=='uncertain-delivery':
                    reconciled = getattr(self,'_reconciled',{})
                    if reconciled.get(row['id'],0) > time.monotonic():
                        continue
                    reconciled[row['id']] = time.monotonic()+3600
                    self._reconciled = reconciled
                    channel=self.client.get_channel(row['channel_id'])
                    if channel is None:
                        try:
                            channel=await self.client.fetch_channel(row['channel_id'])
                        except (discord.HTTPException,TimeoutError,ConnectionError):
                            continue
                    if channel and hasattr(channel,'history'):
                        try:
                            marker=f"reminder:{row['id']}:{int(row['remind_at'])}"
                            async for msg in channel.history(limit=100):
                                if msg.author.id==self.client.user.id and any(e.footer.text==marker for e in msg.embeds):
                                    await asyncio.to_thread(store.complete,row,msg.id)
                                    break
                        except discord.HTTPException:
                            pass
            for row in await asyncio.to_thread(store.claim_due):
                channel=self.client.get_channel(row['channel_id'])
                try:
                    if channel is None:
                        channel=await self.client.fetch_channel(row['channel_id'])
                    embed=discord.Embed(description=row['message'])
                    embed.set_footer(text=f"reminder:{row['id']}:{int(row['remind_at'])}")
                    profile=await asyncio.to_thread(db.get_assistant_profile,row['user_id'])
                    lang=language_for(profile=profile,fallback=row['language'])
                    sent=await channel.send(t('reminder_delivery',lang,user_id=row['user_id'],message=row['message']),embed=embed,view=ReminderControls(row['id'],lang,delivered=True),suppress_embeds=False,allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=row['user_id'])],roles=False,everyone=False))
                    await asyncio.to_thread(store.complete,row,getattr(sent,'id',None))
                    await record_for('scheduled','reminder-delivery',channel)
                except (discord.Forbidden,discord.NotFound) as exc:
                    await asyncio.to_thread(store.fail,row,type(exc).__name__,permanent=True)
                    if channel:
                        await record_for("failure","reminder-delivery",channel)
                except Exception as exc:
                    # Network errors after send are uncertain. Reconcile rather than
                    # automatically duplicating a potentially delivered message.
                    uncertain=isinstance(exc,(TimeoutError,ConnectionError))
                    await asyncio.to_thread(store.fail,row,'uncertain-delivery' if uncertain else type(exc).__name__,permanent=uncertain)
                    if channel:
                        await record_for('failure','reminder-delivery',channel)
            await asyncio.to_thread(store.cleanup)
        except Exception:
            logger.exception('Reminder delivery loop failed')
