"""Owner-scoped product controls attached only to requested private lists."""
import discord
import db
from db.connection import _connect
from db import reminders as actions
from i18n import language_for,t
from command_time import number,CURRENCY_ALIASES


class TargetModal(discord.ui.Modal):
    def __init__(self,user_id,url,lang):
        super().__init__(title=t('edit',lang))
        self.owner=user_id
        self.url=url
        self.lang=lang
        self.price=discord.ui.TextInput(label=t('price_label',lang),placeholder='500,00 / 500.00',required=False)
        self.currency=discord.ui.TextInput(label=t('currency_label',lang),default='RON',max_length=10)
        self.add_item(self.price)
        self.add_item(self.currency)
    async def on_submit(self,interaction):
        if interaction.user.id!=self.owner:
            await interaction.response.send_message(t('not_owner',self.lang),ephemeral=True)
            return
        if not actions.claim_action(f'modal:{interaction.id}'):
            await interaction.response.defer()
            return
        currency=CURRENCY_ALIASES.get(str(self.currency).casefold())
        price=number(str(self.price),self.lang)
        ok=db.set_scraped_item_target(self.owner,self.url,price,currency) if price and price>0 and currency else db.set_scraped_item_target(self.owner,self.url,None,None) if not str(self.price).strip() else False
        actions.finish_action(f'modal:{interaction.id}', bool(ok))
        await interaction.response.send_message(t('saved' if ok else 'number',self.lang),ephemeral=True)


class WishlistManageView(discord.ui.View):
    def __init__(self,user_id, *, language=None):
        super().__init__(timeout=600)
        self.owner=user_id
        self.lang=language or language_for(profile=db.get_assistant_profile(user_id))
        with _connect() as c:
            rows=c.execute('SELECT id,url,title FROM scraped_items WHERE user_id=? ORDER BY id LIMIT 25',(user_id,)).fetchall()
        if not rows:
            return
        self.rows={str(r[0]):r[1] for r in rows}
        self.selected=None
        choice=discord.ui.Select(placeholder=t('select',self.lang),options=[discord.SelectOption(label=f'#{r[0]} {r[2] or r[1]}'[:100],value=str(r[0])) for r in rows])
        async def select(interaction):
            self.selected=choice.values[0]
            await interaction.response.defer()
        choice.callback=select
        self.add_item(choice)
        for op in ('edit','cancel'):
            button=discord.ui.Button(label=t('stop_tracking' if op=='cancel' else op,self.lang))
            async def apply(interaction,operation=op):
                if not self.selected:
                    await interaction.response.send_message(t('select',self.lang),ephemeral=True)
                    return
                url=self.rows[self.selected]
                if operation=='edit':
                    await interaction.response.send_modal(TargetModal(self.owner,url,self.lang))
                else:
                    if not actions.claim_action(f'button:{interaction.id}'):
                        await interaction.response.defer()
                        return
                    ok=db.delete_scraped_item(self.owner,url)
                    actions.finish_action(f'button:{interaction.id}', bool(ok))
                    await interaction.response.send_message(t('saved' if ok else 'not_found',self.lang),ephemeral=True)
            button.callback=apply
            self.add_item(button)
        stock=discord.ui.Button(label=t('restock_control',self.lang))
        async def toggle(interaction):
            if not self.selected:
                await interaction.response.send_message(t('select',self.lang),ephemeral=True)
                return
            if not actions.claim_action(f'button:{interaction.id}'):
                await interaction.response.defer()
                return
            url=self.rows[self.selected]
            with _connect() as c:
                row=c.execute('SELECT restock_only FROM scraped_items WHERE user_id=? AND url=?',(self.owner,url)).fetchone()
            ok=bool(row is not None and db.set_scraped_item_restock_only(self.owner,url,not row[0]))
            actions.finish_action(f'button:{interaction.id}', bool(ok))
            await interaction.response.send_message(t('saved' if ok else 'not_found',self.lang),ephemeral=True)
        stock.callback=toggle
        self.add_item(stock)
    async def interaction_check(self,interaction):
        if interaction.user.id==self.owner:
            return True
        await interaction.response.send_message(t('not_owner',self.lang),ephemeral=True)
        return False
