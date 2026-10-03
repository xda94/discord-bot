"""Explicit private setup and contextual help."""
from __future__ import annotations
import discord
from discord import app_commands
import db
from i18n import language_for,t,localize
from assistant_profiles import effective_profile,validate_timezone

CATEGORIES = {
 'reminders':('Reminders','Remindere','/remind · /reminder-list · /reminder-edit · /reminder-cancel · /reminder-snooze'),
 'wishlist':('Wishlist','Lista de dorințe','/wishlist-item · /wishlist-show · /wishlist-target-price · /wishlist-restock-only · /wishlist-refresh · /wishlist-graph'),
 'flights':('Flights','Zboruri','/flight-tracker-add · /flight-tracker-login · /flight-tracker-show · /flight-tracker-budget'),
 'memory':('Memory','Memorie','/memory-show · /assistant-profile · /assistant-profile-set'),
}


class TimezoneModal(discord.ui.Modal):
    def __init__(self,language):
        super().__init__(title='Fus orar' if language=='ro' else 'Timezone')
        self.language=language
        self.zone=discord.ui.TextInput(label='IANA',placeholder='Europe/Bucharest / UTC',max_length=100)
        self.add_item(self.zone)
    async def on_submit(self,interaction):
        try:
            zone=validate_timezone(str(self.zone))
        except ValueError:
            await interaction.response.send_message(t('timezone',self.language),ephemeral=True)
            return
        saved=db.set_assistant_profile(interaction.user.id,timezone=zone)
        await interaction.response.send_message(t('saved' if saved else 'failed',self.language),ephemeral=True)


class HelpView(discord.ui.View):
    def __init__(self,user_id,language,tree,*,setup=False,memory_disabled=False):
        super().__init__(timeout=600)
        self.user_id=user_id
        self.language=language
        for name,(en,ro,commands) in CATEGORIES.items():
            if name=='memory' and memory_disabled:
                continue
            button=discord.ui.Button(label=ro if language=='ro' else en)
            async def callback(interaction,category=name,content=commands):
                registered={c.name for c in tree.get_commands()}
                content=' · '.join(command for command in content.split(' · ') if command[1:] in registered)
                await interaction.response.send_message(content,ephemeral=True)
            button.callback=callback
            self.add_item(button)
        full=discord.ui.Button(label='Toate comenzile' if language=='ro' else 'Complete reference')
        async def full_callback(interaction):
            lang=self.language
            from features.help_feature import _chunk_text
            lines=[]
            for command in tree.get_commands():
                desc=localize(command.description,lang)
                parameters=' '.join(('[' if not p.required else '<')+p.display_name+(']' if not p.required else '>') for p in command.parameters)
                lines.append(f'**/{command.name}** {parameters}\n{desc}')
            for i,chunk in enumerate(_chunk_text('\n\n'.join(lines))):
                send=interaction.response.send_message if i==0 else interaction.followup.send
                await send(chunk,ephemeral=True)
        full.callback=full_callback
        self.add_item(full)
        if setup:
            for language_code,label in (('ro','Română'),('en','English')):
                button=discord.ui.Button(label=label,row=2)
                async def select_language(interaction,code=language_code):
                    ok=db.set_assistant_profile(interaction.user.id,language=code)
                    await interaction.response.send_message(t('saved' if ok else 'failed',code),view=HelpView(self.user_id,code,tree,setup=True,memory_disabled=memory_disabled),ephemeral=True)
                button.callback=select_language
                self.add_item(button)
            zone=discord.ui.Button(label='Fus orar / Timezone',row=2)
            async def set_zone(interaction):
                await interaction.response.send_modal(TimezoneModal(self.language))
            zone.callback=set_zone
            self.add_item(zone)
    async def interaction_check(self,interaction):
        if interaction.user.id==self.user_id:
            return True
        await interaction.response.send_message(t('not_owner',self.language),ephemeral=True)
        return False


class OnboardingFeature:
    def __init__(self,client,tree,*,memory_disabled=False):
        self.client=client
        self.tree=tree
        @tree.command(name='start',description='Privately set up language, timezone, and explore features')
        async def start(interaction:discord.Interaction):
            lang=language_for(profile=db.get_assistant_profile(interaction.user.id))
            await interaction.response.send_message(t('start',lang),view=HelpView(interaction.user.id,lang,tree,setup=True,memory_disabled=memory_disabled),ephemeral=True)
