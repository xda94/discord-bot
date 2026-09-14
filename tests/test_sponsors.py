import discord
import pytest
from discord import app_commands

from features.sponsors import (
    SPONSOR_TIERS,
    SponsorsFeature,
    build_sponsor_plans_message,
)


@pytest.mark.parametrize("tier", SPONSOR_TIERS.values())
def test_sponsor_plan_message_uses_configured_name_price_and_chance(tier):
    message = build_sponsor_plans_message()

    assert tier["name"] in message
    assert tier["price"] in message
    assert f"{tier['chance']:.0%}" in message


def test_sponsor_plan_message_tracks_tier_price_changes(monkeypatch):
    monkeypatch.setitem(SPONSOR_TIERS["standard"], "price", "99 lei / test")

    assert "99 lei / test" in build_sponsor_plans_message()


def test_sponsor_commands_register_on_python_39(tmp_db):
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)

    SponsorsFeature(client, tree)

    assert tree.get_command("sponsor_plans") is not None
