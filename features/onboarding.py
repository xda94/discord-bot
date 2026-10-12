"""Requester-owned private setup and contextual help."""
from __future__ import annotations
import asyncio
import math
import zoneinfo
import discord
from discord import app_commands
import db
from i18n import language_for,t,localize
from assistant_profiles import validate_timezone

CATEGORIES = {
 'reminders':('Reminders','Remindere','/remind · /reminder-list · /reminder-edit · /reminder-cancel · /reminder-snooze'),
 'wishlist':('Wishlist','Lista de dorințe','/wishlist-item · /wishlist-show · /wishlist-target-price · /wishlist-restock-only · /wishlist-refresh · /wishlist-graph'),
 'flights':('Flights','Zboruri','/flight-tracker-add · /flight-tracker-login · /flight-tracker-show · /flight-tracker-budget'),
 'memory':('Memory','Memorie','/memory-show · /assistant-profile · /assistant-profile-set'),
 'community':('Community games','Jocuri de comunitate','/tribunal · /sedinta · /quest · /bingo · /bingo-obicei · /bingo-obicei-sterge · /citat · /cine-a-zis · /citat-sterge · /scuza · /merita · /horoscop'),
}

COMMUNITY_GUIDE = {
 'en': (
    '**Community games** · Server commands unless stated otherwise. Use slash commands or the Messages menu; these features have no natural-language mention commands. Private language uses your saved profile, then Discord locale; shared sessions keep their creator’s language.\n\n'
    '**/tribunal acuzat:<member> acuzatie:<text>** — An accusation (max 500 characters). Voting opens immediately for all human server members, including the accused. **/sedinta subiect:<text> optiuni:A|B** — Choose 2–5 distinct options. Votes last exactly 600 seconds (10 minutes), survive restart, and each human can change their single vote. Ties/no votes are neutral. Results edit the original message.\n\n'
    '**/quest** — Privately offer a voluntary safe challenge. Accept to publish it, then submit completion within seven days; verification lasts ten minutes. The owner cannot verify themselves. Only a strict positive majority awards a symbolic title; ties/no votes award none. Titles are not Discord roles and remain until an administrator removes them.\n\n'
    '**/bingo** — A shared 3×3 board for the bot’s local ISO week, starting Monday. Mark manually; another human must confirm before it counts. No chat monitoring. **/bingo-obicei text:<Romanian> text-en:<English> [id]** adds/edits habits; **/bingo-obicei-sterge id:<id>** removes one. Manage Server is required; keep 9–50 active habits. Edits affect future boards.\n\n'
    '**Messages → Apps → Pune în muzeu** — Nominate a text message (max 1,200 characters). Its author gets the exact snapshot by DM and has 24 hours to consent. Acceptance shares it guild-wide, including with members unable to see its source channel. Refusal erases unapproved text; blocked DMs cancel nomination. **/citat** shows approved quotes and IDs; **/cine-a-zis** starts a ten-minute author quiz, requiring at least two distinct approved authors. **/citat-sterge id:<id>** lets the author or Manage Messages moderator withdraw. The persistent consent-DM removal control works even after leaving. Removal revokes quizzes and stored text; it cannot recall screenshots or previous ordinary messages.\n\n'
    '**/scuza motiv:<reason> stil:<choice>** — A private excuse in corporate, grandmother, gamer, or public-servant style. **/merita url:<saved-url>** — A purchase advocate and household accountant discuss only your saved wishlist price/stock/history. No refresh or purchase; missing/stale facts are shown honestly, historical currencies are unknown and not converted. Both work in DMs; server acknowledgments are private. Blocked DMs expose no content publicly. Reasons and private generated responses are not saved.\n\n'
    '**/horoscop [tema]** — A deterministic server forecast for general, work, gaming, or household, using the bot’s local date; same member/server/date/theme gives the same result across restart, without an LLM. Bingo, quest selection and quizzes also need no model. Humor uses one bounded model call with a local fallback, including after interrupted result generation. Completed sessions last 30 days after closure; bingo history lasts 30 days after the local ISO week ends; unapproved quote text expires at its consent deadline, approved quotes remain until deletion.'
 ),
 'ro': (
    '**Jocuri de comunitate** · Comenzi pe server, cu excepțiile de mai jos. Folosește comenzile slash sau meniul Mesaje; aceste funcții nu au comenzi în limbaj natural prin mențiuni. Limba privată vine din profil, apoi din Discord; sesiunile comune păstrează limba creatorului.\n\n'
    '**/tribunal acuzat:<membru> acuzatie:<text>** — Acuzație (maximum 500 de caractere). Votul începe imediat pentru toți membrii umani ai serverului, inclusiv acuzatul. **/sedinta subiect:<text> optiuni:A|B** — Alege 2–5 opțiuni distincte. Votul durează exact 600 de secunde (10 minute), supraviețuiește repornirii și fiecare om își poate schimba votul unic. Egalitatea/lipsa voturilor dă rezultat neutru. Rezultatul editează mesajul inițial.\n\n'
    '**/quest** — Primești privat o provocare sigură, voluntară. Accept-o pentru publicare și declară finalizarea în șapte zile; verificarea durează zece minute. Autorul nu se poate verifica singur. Doar majoritatea strict pozitivă acordă un titlu simbolic; egalitatea/lipsa voturilor nu acordă nimic. Titlurile nu sunt roluri Discord și rămân până la ștergerea administrativă.\n\n'
    '**/bingo** — Tablă comună 3×3 pentru săptămâna ISO locală a botului, de luni. Marchezi manual; un alt om confirmă înainte să conteze. Fără monitorizarea conversațiilor. **/bingo-obicei text:<română> text-en:<engleză> [id]** adaugă/editează; **/bingo-obicei-sterge id:<id>** șterge. Necesită Gestionare server; păstrează 9–50 de obiceiuri active. Editările afectează tablele viitoare.\n\n'
    '**Mesaje → Aplicații → Pune în muzeu** — Propui un mesaj text (maximum 1.200 de caractere). Autorul primește copia exactă prin DM și are 24 de ore pentru acord. Acceptarea îl face disponibil întregului server, inclusiv membrilor fără acces la canalul sursă. Refuzul șterge textul neaprobat; DM-urile blocate anulează propunerea. **/citat** arată citate aprobate și ID-uri; **/cine-a-zis** pornește un quiz de zece minute, cu minimum doi autori aprobați distincți. **/citat-sterge id:<id>** permite retragerea de către autor sau moderator cu Gestionare mesaje. Butonul persistent din DM funcționează și după părăsirea serverului. Ștergerea revocă quizurile și textul stocat; nu poate retrage capturi de ecran sau mesaje obișnuite trimise anterior.\n\n'
    '**/scuza motiv:<motiv> stil:<opțiune>** — Scuză privată în stil corporatist, bunică, gamer sau funcționar public. **/merita url:<link-salvat>** — Avocatul cumpărăturii și contabilul casei discută numai prețul/stocul/istoricul din lista ta salvată. Fără actualizare sau cumpărare; datele lipsă/vechi sunt indicate, monedele istorice sunt necunoscute și nu se convertesc. Ambele merg în DM; confirmarea pe server este privată. DM-urile blocate nu expun conținut public. Motivele și răspunsurile private generate nu se salvează.\n\n'
    '**/horoscop [tema]** — Prognoză deterministă pe server pentru general, muncă, gaming sau casă, după data locală a botului; același membru/server/dată/temă dă același rezultat după repornire, fără LLM. Bingo, alegerea questului și quizurile nu folosesc modele. Umorul folosește un singur apel limitat, cu variantă locală de rezervă, inclusiv după întreruperea generării rezultatului. Sesiunile încheiate rămân 30 de zile de la închidere; istoricul bingo rămâne 30 de zile după sfârșitul săptămânii ISO locale; textul citatelor neaprobate expiră la termenul acordului, cele aprobate rămân până la ștergere.'
 ),
}


COMMON_TIMEZONES = (
    'Europe/Bucharest', 'UTC', 'Europe/London', 'Europe/Paris', 'Europe/Berlin',
    'Europe/Rome', 'Europe/Madrid', 'Europe/Athens', 'Europe/Helsinki',
    'Europe/Istanbul', 'America/New_York', 'America/Chicago', 'America/Denver',
    'America/Los_Angeles', 'America/Toronto', 'America/Sao_Paulo', 'Asia/Dubai',
    'Asia/Singapore', 'Asia/Tokyo', 'Australia/Sydney', 'Pacific/Auckland',
)


def display_language(interaction, profile=None):
    locale = str(getattr(interaction, 'locale', 'en')).split('-')[0]
    return language_for(profile=profile, fallback='ro' if locale == 'ro' else 'en')


class HelpView(discord.ui.View):
    def __init__(self,user_id,language,tree,*,memory_disabled=False,bot_mention=None,edit_message=False):
        super().__init__(timeout=600)
        self.user_id=user_id
        self.language=language
        for name,(en,ro,commands) in CATEGORIES.items():
            if name=='memory' and memory_disabled:
                continue
            button=discord.ui.Button(label=ro if language=='ro' else en,row=0)
            async def callback(interaction,category=name,content=commands):
                if not await self.interaction_check(interaction):
                    return
                if category=='community':
                    from features.help_feature import _chunk_text
                    for i,chunk in enumerate(_chunk_text(COMMUNITY_GUIDE[self.language])):
                        if i==0 and edit_message:
                            await interaction.response.edit_message(content=chunk,view=self,allowed_mentions=discord.AllowedMentions.none(),suppress_embeds=True)
                        else:
                            send=interaction.response.send_message if i==0 else interaction.followup.send
                            await send(chunk,ephemeral=True,allowed_mentions=discord.AllowedMentions.none(),suppress_embeds=True)
                    return
                registered={c.name for c in tree.get_commands()}
                content=' · '.join(command for command in content.split(' · ') if command[1:] in registered)
                content=content or t('help_no_commands', self.language)
                if edit_message:
                    await interaction.response.edit_message(content=content,view=self)
                else:
                    await interaction.response.send_message(content,ephemeral=True)
            button.callback=callback
            self.add_item(button)
        full=discord.ui.Button(label='Toate comenzile' if language=='ro' else 'Complete reference',row=1)
        async def full_callback(interaction):
            if not await self.interaction_check(interaction):
                return
            lang=self.language
            from features.help_feature import _chunk_text
            lines=[]
            for command in tree.get_commands():
                if isinstance(command,app_commands.ContextMenu):
                    invocation='Mesaje → Aplicații' if lang=='ro' else 'Messages → Apps'
                    description='Propune un mesaj pentru muzeu; autorul trebuie să accepte prin DM în 24 de ore.' if lang=='ro' else 'Nominate a message for the museum; its author must consent by DM within 24 hours.'
                    lines.append(f'**{invocation} → {command.name}**\n{description}')
                    continue
                if memory_disabled and (command.name.startswith('memory-') or command.name.startswith('llm-memory')):
                    continue
                desc=localize(command.description,lang)
                parameters=' '.join(('[' if not p.required else '<')+p.display_name+(']' if not p.required else '>') for p in command.parameters)
                lines.append(f'**/{command.name}** {parameters}\n{desc}')
            for i,chunk in enumerate(_chunk_text('\n\n'.join(lines) or t('help_no_commands', lang))):
                send=interaction.response.send_message if i==0 else interaction.followup.send
                await send(chunk,ephemeral=True)
        full.callback=full_callback
        self.add_item(full)
        natural=discord.ui.Button(label=t('natural_examples', language),row=2)
        async def natural_callback(interaction):
            if not await self.interaction_check(interaction):
                return
            from features.natural_language_help import NaturalLanguageHelpView, guide_content
            view=NaturalLanguageHelpView(self.user_id,self.language,bot_mention=bot_mention)
            if edit_message:
                # Preserve the setup return control when changing to the guide.
                for child in list(self.children):
                    if child.row==4:
                        self.remove_item(child)
                        view.add_item(child)
                await interaction.response.edit_message(content=guide_content(self.language,bot_mention=bot_mention),view=view)
            else:
                await interaction.response.send_message(guide_content(self.language,bot_mention=bot_mention),view=view,ephemeral=True)
        natural.callback=natural_callback
        self.add_item(natural)
    async def interaction_check(self,interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(t('not_owner',self.language),ephemeral=True)
            return False
        if self.is_finished():
            await interaction.response.send_message(t('setup_expired',self.language),ephemeral=True)
            return False
        return True


class OnboardingView(discord.ui.View):
    """One message, partial writes, and bounded IANA timezone selectors."""
    def __init__(self,user_id,language,tree,*,profile=None,memory_disabled=False,bot_mention=None):
        super().__init__(timeout=600)
        self.user_id=user_id
        self.language=language
        self.tree=tree
        self.profile=profile or {}
        self.memory_disabled=memory_disabled
        self.bot_mention=bot_mention
        self.screen='language'
        self.group=None
        self.page=0
        self.group_page=0
        self.catalog={}
        self.catalog_failed=False
        self.error=None
        self._pending_update=None
        self._revision=0
        self._lock=asyncio.Lock()
        self._build()

    async def interaction_check(self,interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(t('not_owner',self.language),ephemeral=True)
            return False
        if self.is_finished():
            await interaction.response.send_message(t('setup_expired',self.language),ephemeral=True)
            return False
        return True

    def _callback(self,action):
        revision=self._revision
        async def callback(interaction):
            if not await self.interaction_check(interaction):
                return
            async with self._lock:
                if revision != self._revision:
                    await interaction.response.send_message(t('setup_stale',self.language),ephemeral=True)
                    return
                await action(interaction)
        return callback

    def _button(self,key,action,*,row=1,disabled=False):
        button=discord.ui.Button(label=t(key,self.language),row=row,disabled=disabled)
        button.callback=self._callback(action)
        self.add_item(button)

    def _select(self,options,placeholder,action):
        select=discord.ui.Select(options=options,placeholder=t(placeholder,self.language),row=0)
        async def selected(interaction):
            values=(getattr(interaction,'data',None) or {}).get('values',[])
            if not isinstance(values,list) or len(values) != 1 or not isinstance(values[0],str) or values[0] not in {option.value for option in options}:
                await interaction.response.send_message(t('setup_stale',self.language),ephemeral=True)
                return
            await action(interaction,values[0])
        select.callback=self._callback(selected)
        self.add_item(select)

    def _common_zones(self):
        candidates=list(COMMON_TIMEZONES)
        saved=self.profile.get('timezone')
        if saved:
            candidates.append(saved)
        zones=[]
        for candidate in candidates:
            try:
                zone=validate_timezone(candidate)
                if zone not in zones:
                    zones.append(zone)
            except (ValueError,TypeError):
                pass
        if not zones:
            self.catalog_failed=True
            return ['UTC']
        return zones

    def _load_catalog(self):
        self.catalog_failed=False
        try:
            zones=set()
            for zone in sorted(zoneinfo.available_timezones()):
                try:
                    zones.add(validate_timezone(zone))
                except (ValueError,TypeError):
                    continue
            if not zones:
                raise ValueError('Empty timezone catalog')
            groups={}
            for zone in sorted(zones):
                group=zone.split('/',1)[0] if '/' in zone else 'Other'
                groups.setdefault(group,[]).append(zone)
            self.catalog=groups
        except Exception:
            self.catalog={'Other':['UTC']}
            self.catalog_failed=True

    def _build(self):
        self.clear_items()
        self._revision+=1
        if self.screen=='language':
            labels=(('en','English'),('ro','Română'),('auto',t('automatic',self.language)))
            self._select([discord.SelectOption(label=label,value=code,default=code==self.profile.get('language','auto')) for code,label in labels],'choose_language',self._save_language)
        elif self.screen=='timezone':
            saved=self.profile.get('timezone') if self.profile.get('timezone_configured') else None
            self._select([discord.SelectOption(label=zone,value=zone,default=zone==saved) for zone in self._common_zones()],'choose_timezone',self._save_timezone)
            self._button('back',self._language)
            self._button('browse_timezones',self._browse)
            self._button('skip_now',self._ready)
        elif self.screen=='groups':
            groups=sorted(self.catalog)
            pages=math.ceil(len(groups)/25)
            self.group_page=min(max(0,self.group_page),pages-1)
            current=groups[self.group_page*25:(self.group_page+1)*25]
            self._select([discord.SelectOption(label=t('other_timezones',self.language) if group=='Other' else group,value=group) for group in current],'choose_region',self._locations)
            self._button('back',self._timezone)
            if pages>1:
                self._button('previous_page',self._previous,disabled=self.group_page==0)
                self._button('next_page',self._next,disabled=self.group_page+1==pages)
            self._button('skip_now',self._ready)
            if self.catalog_failed:
                self._button('retry',self._browse)
        elif self.screen=='locations':
            zones=self.catalog[self.group]
            pages=math.ceil(len(zones)/25)
            self.page=min(max(0,self.page),pages-1)
            current=zones[self.page*25:(self.page+1)*25]
            saved=self.profile.get('timezone') if self.profile.get('timezone_configured') else None
            self._select([discord.SelectOption(label=zone,value=zone,default=zone==saved) for zone in current],'choose_timezone',self._save_timezone)
            self._button('back',self._groups)
            self._button('previous_page',self._previous,disabled=self.page==0)
            self._button('next_page',self._next,disabled=self.page+1==pages)
            self._button('skip_now',self._ready)
        elif self.screen=='ready':
            self._button('change_language',self._language,row=0)
            self._button('change_timezone',self._timezone,row=0)
            self._button('explore_features',self._help,row=1)
            self._button('natural_examples',self._guide,row=1)
        if self.error and self._pending_update:
            async def retry(interaction):
                await self._save(interaction,**self._pending_update)
            self._button('retry',retry,row=2)

    def content(self):
        lang=self.language
        language_label={'en':'English','ro':'Română','auto':t('automatic',lang)}.get(self.profile.get('language','auto'),t('automatic',lang))
        if self.screen=='language':
            content=t('setup_language',lang,language_name=language_label)
        elif self.screen=='ready':
            zone=self.profile.get('timezone','UTC') if self.profile.get('timezone_configured') else t('timezone_unconfigured',lang)
            content=t('setup_ready',lang,language_name=language_label,timezone=zone)
            if not self.profile.get('timezone_configured'):
                content+='\n\n'+t('setup_skip_guidance',lang)
        else:
            content=t('setup_timezone',lang)
            if self.profile.get('timezone_configured'):
                content+='\n'+t('setup_current_timezone',lang,timezone=self.profile.get('timezone','UTC'))
            if self.screen=='groups':
                content+='\n'+t('setup_regions',lang)
            elif self.screen=='locations':
                content+='\n'+t('setup_page',lang,region=self.group,page=self.page+1,pages=math.ceil(len(self.catalog[self.group])/25))
            if self.catalog_failed:
                content+='\n\n'+t('timezone_catalog_failed',lang)
        if self.error:
            content+='\n\n'+self.error
        return content

    async def _show(self,interaction,screen):
        try:
            profile=db.get_assistant_profile(self.user_id) or {}
        except Exception:
            await self._failed(interaction)
            return
        self.profile=profile
        self.language=display_language(interaction,profile)
        self.screen=screen
        self.error=None
        self._pending_update=None
        self._build()
        await interaction.response.edit_message(content=self.content(),view=self)

    async def _failed(self,interaction):
        self.error=t('setup_save_failed',self.language)
        self._build()
        await interaction.response.edit_message(content=self.content(),view=self)

    async def _save(self,interaction,**update):
        self._pending_update=update
        try:
            saved=db.set_assistant_profile(self.user_id,**update)
        except Exception:
            saved=None
        if not saved:
            await self._failed(interaction)
            return
        await self._show(interaction,'timezone' if 'language' in update else 'ready')

    async def _save_language(self,interaction,code):
        await self._save(interaction,language=code)

    async def _save_timezone(self,interaction,zone):
        self._pending_update={'timezone':zone}
        try:
            zone=validate_timezone(zone)
        except (ValueError,TypeError):
            await self._failed(interaction)
            return
        await self._save(interaction,timezone=zone)

    async def _language(self,interaction):
        await self._show(interaction,'language')

    async def _timezone(self,interaction):
        await self._show(interaction,'timezone')

    async def _ready(self,interaction):
        await self._show(interaction,'ready')

    async def _browse(self,interaction):
        self._load_catalog()
        self.group_page=0
        await self._show(interaction,'groups')

    async def _groups(self,interaction):
        await self._show(interaction,'groups')

    async def _locations(self,interaction,group):
        self.group=group
        self.page=0
        await self._show(interaction,'locations')

    async def _previous(self,interaction):
        if self.screen=='groups':
            self.group_page-=1
        else:
            self.page-=1
        await self._show(interaction,self.screen)

    async def _next(self,interaction):
        if self.screen=='groups':
            self.group_page+=1
        else:
            self.page+=1
        await self._show(interaction,self.screen)

    def _return_button(self,view):
        button=discord.ui.Button(label=t('back_to_setup',self.language),row=4)
        button.callback=self._callback(self._ready)
        view.add_item(button)
        return view

    async def _help(self,interaction):
        view=HelpView(self.user_id,self.language,self.tree,memory_disabled=self.memory_disabled,bot_mention=self.bot_mention,edit_message=True)
        await interaction.response.edit_message(content=t('help',self.language),view=self._return_button(view))

    async def _guide(self,interaction):
        from features.natural_language_help import NaturalLanguageHelpView,guide_content
        view=NaturalLanguageHelpView(self.user_id,self.language,bot_mention=self.bot_mention)
        await interaction.response.edit_message(content=guide_content(self.language,bot_mention=self.bot_mention),view=self._return_button(view))


class OnboardingFeature:
    def __init__(self,client,tree,*,memory_disabled=False):
        self.client=client
        self.tree=tree
        @tree.command(name='start',description='Privately set up language, timezone, and explore features')
        async def start(interaction:discord.Interaction):
            profile=db.get_assistant_profile(interaction.user.id)
            lang=display_language(interaction,profile)
            mention=getattr(getattr(client,'user',None),'mention',None)
            view=OnboardingView(interaction.user.id,lang,tree,profile=profile,memory_disabled=memory_disabled,bot_mention=mention)
            await interaction.response.send_message(view.content(),view=view,ephemeral=True)
