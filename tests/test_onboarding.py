"""Private setup, timezone browsing, and partial preference updates."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands

import db
from features import onboarding
from i18n import t


def interaction(user_id=1, locale='en-US', values=None):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id), locale=locale,
        data={'values': values} if values is not None else {},
        response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def tree(*names):
    return SimpleNamespace(get_commands=lambda: [
        SimpleNamespace(name=name, description='Set a reminder', parameters=[])
        for name in names
    ])


def view(language='en', **kwargs):
    return onboarding.OnboardingView(1, language, tree('remind', 'natural-language'), **kwargs)


async def select(current, value, **kwargs):
    event=interaction(values=[value], **kwargs)
    await next(child for child in current.children if isinstance(child, discord.ui.Select)).callback(event)
    return event


async def button(current, key, **kwargs):
    event=interaction(**kwargs)
    await next(child for child in current.children if getattr(child,'label',None)==t(key,current.language)).callback(event)
    return event


def test_start_registers_private_language_screen_without_writes(tmp_db):
    async def run():
        client=discord.Client(intents=discord.Intents.none())
        commands=app_commands.CommandTree(client)
        onboarding.OnboardingFeature(client,commands)
        event=interaction(locale='ro')
        await commands.get_command('start').callback(event)
        args=event.response.send_message.await_args
        current=args.kwargs['view']
        assert args.kwargs['ephemeral'] is True
        assert current.timeout==600 and current.screen=='language'
        assert current.language=='ro'
        assert {option.value for option in current.children[0].options}=={'en','ro','auto'}
        assert next(option for option in current.children[0].options if option.default).value=='auto'
        assert db.get_assistant_profile(1) is None
    asyncio.run(run())


@pytest.mark.parametrize('code,locale,expected',[
    ('en','ro','en'), ('ro','en-US','ro'), ('auto','ro','ro'),
    ('auto','fr','en'), ('auto','en-GB','en'),
])
def test_language_selection_updates_only_language_and_same_message(tmp_db,code,locale,expected):
    original=db.set_assistant_profile(1,language='ro',timezone='Europe/Bucharest',currency='RON',tone='formal',llm_behavior='detailed',notification_style='compact',quiet_start='22:00',quiet_end='08:00',delivery_mode='daily',digest_time='10:00')
    async def run():
        current=view(profile=original)
        event=await select(current,code,locale=locale)
        saved=db.get_assistant_profile(1)
        assert saved==dict(original,language=code)
        assert current.language==expected and current.screen=='timezone'
        assert event.response.edit_message.await_args.kwargs['view'] is current
        event.response.send_message.assert_not_awaited()
        values=[option.value for option in current.children[0].options]
        assert values==list(onboarding.COMMON_TIMEZONES)
        assert next(option for option in current.children[0].options if option.default).value=='Europe/Bucharest'
    asyncio.run(run())


def test_skip_does_not_write_timezone_and_distinguishes_unconfirmed_utc(tmp_db,monkeypatch):
    async def run():
        current=view()
        await select(current,'en')
        original=db.get_assistant_profile(1)
        setter=Mock(wraps=db.set_assistant_profile)
        monkeypatch.setattr(db,'set_assistant_profile',setter)
        event=await button(current,'skip_now')
        setter.assert_not_called()
        assert db.get_assistant_profile(1)==original
        assert current.screen=='ready'
        text=event.response.edit_message.await_args.kwargs['content']
        assert 'unconfirmed' in text and 'in one hour' in text and 'tomorrow at 9' in text
        await button(current,'change_timezone')
        await select(current,'UTC')
        assert db.get_assistant_profile(1)['timezone_configured']==1
        assert '**UTC**' in current.content() and 'unconfirmed' not in current.content()
        assert setter.call_args.kwargs=={'timezone':'UTC'}
    asyncio.run(run())


def test_existing_profile_skip_and_reopen_reread_saved_preferences(tmp_db):
    initial=db.set_assistant_profile(1,language='ro',timezone='Europe/Bucharest',tone='playful')
    async def run():
        current=view('ro',profile=initial)
        await select(current,'ro')
        db.set_assistant_profile(1,language='en',timezone='Europe/Lisbon')
        await button(current,'skip_now')
        assert current.language=='en'
        assert 'Europe/Lisbon' in current.content() and 'Relative reminders' not in current.content()
        await button(current,'change_language')
        assert next(option for option in current.children[0].options if option.default).value=='en'
        assert db.get_assistant_profile(1)['tone']=='playful'
    asyncio.run(run())


def test_common_timezones_validate_and_dedupe_saved_zone(tmp_db,monkeypatch):
    async def run():
        current=view(profile={'timezone':'Europe/Lisbon','timezone_configured':1})
        assert current._common_zones()[-1]=='Europe/Lisbon'
        current.profile={'timezone':'UTC'}
        assert current._common_zones().count('UTC')==1
        current.profile={'timezone':'Mars/Olympus'}
        assert 'Mars/Olympus' not in current._common_zones()
        monkeypatch.setattr(onboarding,'COMMON_TIMEZONES',('UTC','UTC','Mars/Olympus'))
        assert current._common_zones()==['UTC']
    asyncio.run(run())


def test_timezone_browser_grouping_paging_and_no_slash_identifiers(tmp_db,monkeypatch):
    real_zones=sorted(zone for zone in onboarding.zoneinfo.available_timezones() if zone.startswith('Europe/'))[:52]
    monkeypatch.setattr(onboarding.zoneinfo,'available_timezones',lambda: set(real_zones+['UTC','CET','Invalid/Place']))
    async def run():
        current=view()
        await select(current,'en')
        await button(current,'browse_timezones')
        assert set(current.catalog)=={'Europe','Other'}
        assert 'Invalid/Place' not in sum(current.catalog.values(),[])
        await select(current,'Europe')
        assert len(current.children[0].options)==25
        stale_selector=current.children[0]
        await button(current,'next_page')
        assert len(current.children[0].options)==25 and 'page 2 of 3' in current.content()
        stale=interaction(values=[real_zones[0]])
        await stale_selector.callback(stale)
        assert 'no longer valid' in stale.response.send_message.await_args.args[0]
        invalid=await select(current,real_zones[0])
        invalid.response.edit_message.assert_not_awaited()
        assert db.get_assistant_profile(1)['timezone_configured']==0
        await button(current,'next_page')
        assert len(current.children[0].options)==2
        assert next(child for child in current.children if getattr(child,'label',None)=='Next').disabled
        await button(current,'previous_page')
        await button(current,'back')
        await select(current,'Other')
        assert {option.value for option in current.children[0].options}=={'CET','UTC'}
        await select(current,'CET')
        assert current.screen=='ready' and db.get_assistant_profile(1)['timezone']=='CET'
    asyncio.run(run())


def test_region_selector_paging_stays_under_discord_limit(tmp_db,monkeypatch):
    monkeypatch.setattr(onboarding.zoneinfo,'available_timezones',lambda: {f'Region{i:02}/City' for i in range(52)})
    monkeypatch.setattr(onboarding,'validate_timezone',lambda zone: zone)
    async def run():
        current=view()
        await select(current,'en')
        await button(current,'browse_timezones')
        assert len(current.children[0].options)==25
        await button(current,'next_page')
        assert len(current.children[0].options)==25
        await button(current,'next_page')
        assert len(current.children[0].options)==2
        await button(current,'previous_page')
        assert len(current.children[0].options)==25
    asyncio.run(run())


@pytest.mark.parametrize('failure',['exception','empty','invalid'])
def test_catalog_failure_visible_utc_fallback_and_retry(tmp_db,monkeypatch,failure):
    def catalog():
        if failure=='exception':
            raise RuntimeError('unavailable')
        return set() if failure=='empty' else {'Mars/Olympus'}
    monkeypatch.setattr(onboarding.zoneinfo,'available_timezones',catalog)
    async def run():
        current=view()
        await select(current,'en')
        await button(current,'browse_timezones')
        assert current.catalog=={'Other':['UTC']}
        assert 'catalog is unavailable' in current.content()
        await button(current,'retry')
        assert current.screen=='groups'
        await select(current,'Other')
        assert 'catalog is unavailable' in current.content()
        await select(current,'UTC')
        assert current.screen=='ready' and db.get_assistant_profile(1)['timezone_configured']==1
    asyncio.run(run())


@pytest.mark.parametrize('screen',['language','timezone','groups','locations','ready'])
def test_every_setup_screen_is_owner_checked_before_writes(tmp_db,screen):
    async def run():
        current=view()
        current.screen=screen
        current.catalog={'Europe':['Europe/Bucharest']}
        current.group='Europe'
        current._build()
        event=interaction(user_id=2,values=['ro'])
        await current.children[0].callback(event)
        assert 'another user' in event.response.send_message.await_args.args[0]
        event.response.edit_message.assert_not_awaited()
        assert db.get_assistant_profile(1) is None
        assert db.get_assistant_profile(2) is None
    asyncio.run(run())


@pytest.mark.parametrize('values',[[],['de'],['ro','en'],['Europe/Bucharest']])
def test_invalid_language_selection_does_not_write(tmp_db,values):
    async def run():
        current=view()
        event=interaction(values=values)
        await current.children[0].callback(event)
        assert db.get_assistant_profile(1) is None
        event.response.send_message.assert_awaited_once()
        assert current.screen=='language'
    asyncio.run(run())


@pytest.mark.parametrize('field',['language','timezone'])
@pytest.mark.parametrize('failure',['none','exception'])
def test_save_failure_retains_screen_and_offers_owned_retry(tmp_db,monkeypatch,field,failure):
    original=db.set_assistant_profile(1,language='en',timezone='Europe/Bucharest',currency='EUR',tone='formal')
    setter=db.set_assistant_profile
    async def run():
        current=view(profile=original)
        if field=='timezone':
            await select(current,'en')
        screen=current.screen
        monkeypatch.setattr(db,'set_assistant_profile',Mock(return_value=None,side_effect=RuntimeError('failure') if failure=='exception' else None))
        await select(current,'ro' if field=='language' else 'UTC')
        assert current.screen==screen and 'could not save' in current.content()
        assert db.get_assistant_profile(1)==original
        retry=next(child for child in current.children if getattr(child,'label',None)=='Retry')
        bad=interaction(user_id=2)
        await retry.callback(bad)
        bad.response.edit_message.assert_not_awaited()
        monkeypatch.setattr(db,'set_assistant_profile',Mock(wraps=setter))
        event=interaction()
        await retry.callback(event)
        assert db.set_assistant_profile.call_args.kwargs=={field:'ro' if field=='language' else 'UTC'}
        assert current.screen==('timezone' if field=='language' else 'ready')
        assert db.get_assistant_profile(1)['currency']=='EUR'
        event.response.send_message.assert_not_awaited()
    asyncio.run(run())


def test_expired_and_stale_controls_do_not_write(tmp_db):
    async def run():
        current=view()
        stale=current.children[0]
        await select(current,'en')
        event=interaction(values=['ro'])
        await stale.callback(event)
        assert db.get_assistant_profile(1)['language']=='en'
        assert 'no longer valid' in event.response.send_message.await_args.args[0]
        current.stop()
        event=await select(current,'UTC')
        assert 'expired' in event.response.send_message.await_args.args[0]
        assert db.get_assistant_profile(1)['timezone_configured']==0
    asyncio.run(run())


def test_timezone_update_retains_notification_rescheduling(tmp_db):
    from db import notifications
    event_id=notifications.enqueue(1,'flight',7,'price','Price changed','en',9999999999)
    async def run():
        current=view()
        await select(current,'en')
        await select(current,'Europe/Bucharest')
        with db._connect() as connection:
            due=connection.execute('SELECT due_at FROM notification_outbox WHERE id=?',(event_id,)).fetchone()[0]
        assert due<9999999999
    asyncio.run(run())


def test_ready_feature_and_natural_guide_navigation_edit_same_message(tmp_db):
    async def run():
        current=view(bot_mention='<@99>')
        await select(current,'en')
        await button(current,'skip_now')
        event=await button(current,'explore_features')
        help_view=event.response.edit_message.await_args.kwargs['view']
        category=interaction()
        await help_view.children[0].callback(category)
        assert '/remind' in category.response.edit_message.await_args.kwargs['content']
        category.response.send_message.assert_not_awaited()
        guide_event=await button(help_view,'natural_examples')
        guide=guide_event.response.edit_message.await_args.kwargs['view']
        assert '<@99>' in guide_event.response.edit_message.await_args.kwargs['content']
        returned=await button(guide,'back_to_setup')
        assert returned.response.edit_message.await_args.kwargs['view'] is current
        event=await button(current,'natural_examples')
        assert event.response.edit_message.await_args.kwargs['view'].user_id==1
        event.response.send_message.assert_not_awaited()
    asyncio.run(run())


def test_help_reference_discovers_commands_and_filters_disabled_memory(tmp_db):
    async def run():
        current=onboarding.HelpView(1,'en',tree('remind','natural-language','memory-show','llm-memory','llm-memory-purge'),memory_disabled=True)
        assert 'Memory' not in [getattr(child,'label',None) for child in current.children]
        event=interaction()
        full=next(child for child in current.children if getattr(child,'label',None)=='Complete reference')
        await full.callback(event)
        content=event.response.send_message.await_args.args[0]
        assert '/natural-language' in content
        assert '/memory-' not in content and '/llm-memory' not in content
        bad=interaction(user_id=2)
        await full.callback(bad)
        assert 'another user' in bad.response.send_message.await_args.args[0]
    asyncio.run(run())
