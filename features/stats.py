import asyncio
import logging
import os
import time
from datetime import timedelta

import discord
import psutil
from discord import app_commands

logger = logging.getLogger("discord_bot")


def _collect_stats() -> str:
    """Gather all host metrics and format the `/stats` reply. Pure-blocking
    — `psutil.cpu_percent(interval=1)` alone sleeps for a full second, plus
    several syscalls for disk/net/temp. Runs in a worker thread via
    `asyncio.to_thread` so the bot's event loop isn't frozen during that
    second."""
    cpu_percent = psutil.cpu_percent(interval=1)
    cpu_freq = psutil.cpu_freq()
    freq_str = f"{cpu_freq.current:.0f}MHz" if cpu_freq else "N/A"
    try:
        load_1, load_5, load_15 = os.getloadavg()
        load_str = f"{load_1:.2f} / {load_5:.2f} / {load_15:.2f}"
    except (AttributeError, OSError):
        # Windows does not provide getloadavg().
        load_str = "N/A"

    mem = psutil.virtual_memory()
    mem_used = mem.used / (1024 ** 2)
    mem_total = mem.total / (1024 ** 2)

    disk = psutil.disk_usage("/")
    disk_used = disk.used / (1024 ** 2)
    disk_total = disk.total / (1024 ** 2)

    temps = psutil.sensors_temperatures() if hasattr(psutil, "sensors_temperatures") else {}
    if temps:
        first_sensor = next(iter(temps.values()))
        temp_str = f"{first_sensor[0].current:.1f}°C"
    else:
        temp_str = "N/A"

    boot_time = psutil.boot_time()
    uptime = timedelta(seconds=time.time() - boot_time)
    days = uptime.days
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes = remainder // 60
    uptime_str = f"{days}d {hours}h {minutes}m"

    net = psutil.net_io_counters()
    net_sent = net.bytes_sent / (1024 ** 3)
    net_recv = net.bytes_recv / (1024 ** 3)

    proc = psutil.Process()
    bot_mem = proc.memory_info().rss / (1024 ** 2)

    return (
        "**System Stats**\n"
        f"🌡️ Temperature: {temp_str}\n"
        f"🖥️ CPU: {cpu_percent}% @ {freq_str} | Load: {load_str}\n"
        f"🧠 RAM: {mem_used:.0f} / {mem_total:.0f} MB ({mem.percent}%)\n"
        f"💾 Disk: {disk_used:.0f} / {disk_total:.0f} MB ({disk.percent}%)\n"
        f"🌐 Network: ↑ {net_sent:.2f} GB / ↓ {net_recv:.2f} GB\n"
        f"⏱️ Uptime: {uptime_str}\n"
        f"🤖 Bot memory: {bot_mem:.1f} MB"
    )


class StatsFeature:
    """The /stats command — reports host machine + bot process metrics."""

    def __init__(self, client: discord.Client, tree: app_commands.CommandTree):
        self.client = client
        self.tree = tree
        self._register_commands()

    def _register_commands(self) -> None:
        @self.tree.command(
            name="stats",
            description="Show hardware stats for the machine running the bot",
        )
        async def stats(interaction: discord.Interaction):
            logger.info(f"Command /stats called by {interaction.user}")
            # Defer because the worker takes ~1s; without `defer` the user
            # would race Discord's 3-second initial-response window on a
            # busy host.
            await interaction.response.defer()
            text = await asyncio.to_thread(_collect_stats)
            await interaction.followup.send(text)
