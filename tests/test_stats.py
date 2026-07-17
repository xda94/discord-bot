import asyncio
from types import SimpleNamespace

import discord
from discord import app_commands

import features.stats as stats_module


def test_temperature_prefers_cpu_sensor(monkeypatch):
    monkeypatch.setattr(
        stats_module.psutil,
        "sensors_temperatures",
        lambda: {
            "gpu": [SimpleNamespace(current=70.0)],
            "coretemp": [SimpleNamespace(current=52.5)],
        },
        raising=False,
    )
    assert stats_module._temperature_string() == "52.5°C"


def test_collect_stats_degrades_to_na_when_platform_metrics_fail(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("not supported on this host")

    for name in (
        "cpu_percent",
        "cpu_freq",
        "cpu_count",
        "virtual_memory",
        "disk_usage",
        "boot_time",
        "net_io_counters",
        "Process",
    ):
        monkeypatch.setattr(stats_module.psutil, name, unavailable)
    monkeypatch.setattr(
        stats_module.psutil, "sensors_temperatures", unavailable, raising=False
    )
    monkeypatch.setattr(stats_module.os, "getloadavg", unavailable, raising=False)

    result = stats_module._collect_stats()

    assert "**System Stats**" in result
    assert "Temperature: N/A" in result
    assert "CPU: N/A @ N/A | Cores: N/A" in result
    assert "Load (1/5/15m): N/A" in result
    assert "RAM: N/A" in result
    assert "Network: N/A" in result
    assert "Host uptime: N/A" in result
    assert "Bot memory: N/A" in result


def test_disk_target_uses_current_drive_root():
    target = stats_module._disk_target()
    assert target
    assert target == stats_module.Path.cwd().anchor or target == stats_module.os.path.abspath(
        stats_module.os.sep
    )


class FakeResponse:
    def __init__(self):
        self.deferred = False

    async def defer(self):
        self.deferred = True


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content):
        self.messages.append(content)


class FakeInteraction:
    def __init__(self):
        self.user = "tester"
        self.response = FakeResponse()
        self.followup = FakeFollowup()


def test_stats_command_returns_friendly_error_if_collector_crashes(monkeypatch):
    def crash():
        raise RuntimeError("unexpected")

    monkeypatch.setattr(stats_module, "_collect_stats", crash)
    client = discord.Client(intents=discord.Intents.none())
    tree = app_commands.CommandTree(client)
    stats_module.StatsFeature(client, tree)
    interaction = FakeInteraction()

    asyncio.run(tree.get_command("stats").callback(interaction))

    assert interaction.response.deferred is True
    assert "Could not collect system stats" in interaction.followup.messages[0]
    asyncio.run(client.close())
