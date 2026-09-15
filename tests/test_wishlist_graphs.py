import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

import db
from features import wishlist_graphs as graphs
from features.scraping import ScrapingFeature


def _interaction(user_id=123):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock(),
    )


def test_graph_filters_custom_period_and_ownership(tmp_db, monkeypatch):
    now = 1_800_000_000
    monkeypatch.setattr(graphs.time, "time", lambda: now)
    url = "https://example.com/a"
    own = db.add_scraped_item(123, url, title="Own", currency="RON")
    other = db.add_scraped_item(456, url, title="Other", currency="RON")
    with db._connect(commit=True) as c:
        for item_id, price, age in [(own, 100, 50), (own, 80, 14), (own, 90, 1), (other, 600, 1)]:
            c.execute("INSERT INTO price_history (item_id, price, timestamp) VALUES (?, ?, ?)",
                      (item_id, price, now - age * 86400))
    renderer = MagicMock(return_value=b"png")
    monkeypatch.setattr(graphs, "render_multi_price_history_png", renderer)
    converter = SimpleNamespace(to_currency=lambda price, *_: price)
    content, file = graphs.build_graph(123, None, "RON", 14, True, converter, lambda cur, _: cur)
    try:
        series = renderer.call_args.args[0]
        assert len(series) == 1
        assert series[0][0] == "Own"
        assert [price for _, price in series[0][1]] == [80, 90]
        assert "14 days" in content
        assert renderer.call_args.kwargs["percentage"] is True
    finally:
        file.close()
    renderer.reset_mock()
    content, file = graphs.build_graph(123, url, "RON", 1, False, converter, lambda cur, _: cur)
    if file:
        file.close()
    content, file = graphs.build_graph(999, url, "RON", 14, False, converter, lambda cur, _: cur)
    assert file is None and "not in your tracking list" in content


def test_percentage_skips_zero_baseline_but_does_not_need_exchange_rates(tmp_db, monkeypatch):
    url = "https://example.com/zero"
    item_id = db.add_scraped_item(123, url)
    db.add_price_history(item_id, 0)
    db.add_price_history(item_id, 100)
    converter = MagicMock()
    text, file = graphs.build_graph(123, None, "RON", 180, True, converter, lambda *_: None)
    assert file is None and "positive starting price" in text
    converter.to_currency.assert_not_called()


def test_graph_buttons_custom_days_and_percentage_replace_original_attachment():
    async def run():
        build = MagicMock(side_effect=lambda days, percentage: (
            f"{days} days / {percentage}", discord.File(io.BytesIO(b"png"), filename="graph.png")
        ))
        view = graphs.WishlistGraphView(123, build, allow_percentage=True)
        interaction = _interaction()
        await view.children[0].callback(interaction)
        assert build.call_args.args == (7, False)
        assert interaction.edit_original_response.await_args.kwargs["attachments"][0].filename == "graph.png"
        await view.children[3].callback(interaction)
        modal = interaction.response.send_modal.await_args.args[0]
        modal.days_input._value = "45"
        await modal.on_submit(interaction)
        assert build.call_args.args == (45, False)
        await view.children[4].callback(interaction)
        assert build.call_args.args == (45, True)
        assert view.children[4].label == "Show prices"
        # A period without data removes the old image but leaves navigation.
        build.return_value = ("No observations", None)
        build.side_effect = None
        await view.update_graph(interaction, days=1)
        assert interaction.edit_original_response.await_args.kwargs["attachments"] == []
        view.stop()
    asyncio.run(run())


def test_graph_controls_reject_other_users_invalid_days_and_expired_modal():
    async def run():
        build = MagicMock()
        view = graphs.WishlistGraphView(123, build)
        stranger = _interaction(456)
        await view.update_graph(stranger, days=30)
        build.assert_not_called()
        stranger.response.send_message.assert_awaited_once()
        owner = _interaction()
        modal = graphs.CustomDaysModal(view)
        for value in ["0", "181", "abc", "1.5", "-1"]:
            modal.days_input._value = value
            await modal.on_submit(owner)
        build.assert_not_called()
        assert owner.response.send_message.await_count == 5
        view.stop()
        modal.days_input._value = "45"
        await modal.on_submit(owner)
        assert "expired" in owner.response.send_message.await_args.args[0]
        build.assert_not_called()
    asyncio.run(run())


def test_render_failure_keeps_previous_view_and_allows_retry():
    async def run():
        build = MagicMock(side_effect=RuntimeError("render failed"))
        view = graphs.WishlistGraphView(123, build, days=30, allow_percentage=True)
        interaction = _interaction()
        await view.update_graph(interaction, days=7, toggle=True)
        assert (view.days, view.percentage) == (30, False)
        interaction.edit_original_response.assert_not_awaited()
        interaction.followup.send.assert_awaited_once()
        assert not view._lock.locked()
        view.stop()
    asyncio.run(run())


def test_graph_slash_options_accept_custom_days_and_percentage(tmp_db, monkeypatch):
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    ScrapingFeature(client, tree)
    send = AsyncMock()
    monkeypatch.setattr("features.scraping.send_graph", send)
    interaction = _interaction()
    for name in ["wishlist-graph", "wishlist-graph-all"]:
        command = tree.get_command(name)
        param = next(p for p in command.parameters if p.name == "days")
        assert (param.min_value, param.max_value, param.default) == (1, 180, 180)
    asyncio.run(tree.get_command("wishlist-graph-all").callback(interaction, days=45, percentage=True))
    assert send.await_args.kwargs["days"] == 45
    assert send.await_args.kwargs["percentage"] is True
    asyncio.run(tree.get_command("wishlist-graph").callback(interaction, "https://example.com/a", days=12))
    assert send.await_args.kwargs["days"] == 12
