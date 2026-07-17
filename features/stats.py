from __future__ import annotations

import asyncio
import logging
import os
import platform
import time
from datetime import timedelta
from pathlib import Path

import discord
import psutil
from discord import app_commands

logger = logging.getLogger("discord_bot")


def _safe_call(func, default=None, *args, **kwargs):
    """Call one optional platform metric without breaking the whole report."""
    try:
        return func(*args, **kwargs)
    except (AttributeError, FileNotFoundError, NotImplementedError, OSError, RuntimeError, ValueError):
        return default


def _format_bytes(value: float | int | None) -> str:
    if value is None:
        return "N/A"
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(amount) < 1024.0 or unit == "PB":
            decimals = 0 if unit in ("B", "KB", "MB") else 2
            return f"{amount:.{decimals}f} {unit}"
        amount /= 1024.0
    return "N/A"


def _disk_target() -> str:
    """Return the current filesystem root on Windows, Linux, or macOS."""
    try:
        anchor = Path.cwd().anchor
    except OSError:
        anchor = ""
    return anchor or os.path.abspath(os.sep)


def _temperature_string() -> str:
    sensor_func = getattr(psutil, "sensors_temperatures", None)
    if sensor_func is None:
        return "N/A"
    temperatures = _safe_call(sensor_func, {}) or {}

    # Prefer conventional CPU sensor groups, then fall back to any sane
    # temperature. This works for Raspberry Pi/Linux while degrading cleanly
    # on Windows and virtual/container hosts where no sensor API is exposed.
    preferred = ("cpu_thermal", "coretemp", "k10temp", "zenpower", "acpitz")
    ordered_groups = []
    for name in preferred:
        if name in temperatures:
            ordered_groups.append(temperatures[name])
    ordered_groups.extend(
        entries for name, entries in temperatures.items() if name not in preferred
    )
    for entries in ordered_groups:
        for reading in entries or ():
            current = getattr(reading, "current", None)
            if isinstance(current, (int, float)) and -50 <= current <= 200:
                return f"{current:.1f}°C"
    return "N/A"


def _collect_stats() -> str:
    """Gather portable host metrics for `/stats` without all-or-nothing failure."""
    cpu_percent = _safe_call(psutil.cpu_percent, None, interval=0.5)
    cpu_percent_str = f"{cpu_percent:.1f}%" if cpu_percent is not None else "N/A"
    cpu_freq = _safe_call(psutil.cpu_freq)
    current_freq = getattr(cpu_freq, "current", None)
    freq_str = f"{current_freq:.0f} MHz" if current_freq and current_freq > 0 else "N/A"
    logical_cores = _safe_call(psutil.cpu_count, None, logical=True)
    physical_cores = _safe_call(psutil.cpu_count, None, logical=False)
    core_parts = []
    if physical_cores:
        core_parts.append(f"{physical_cores} physical")
    if logical_cores:
        core_parts.append(f"{logical_cores} logical")
    core_str = ", ".join(core_parts) if core_parts else "N/A"

    load_func = getattr(os, "getloadavg", None)
    load_average = _safe_call(load_func) if load_func is not None else None
    load_str = (
        f"{load_average[0]:.2f} / {load_average[1]:.2f} / {load_average[2]:.2f}"
        if load_average and len(load_average) == 3
        else "N/A"
    )

    mem = _safe_call(psutil.virtual_memory)
    if mem is None:
        memory_str = "N/A"
    else:
        memory_str = (
            f"{_format_bytes(mem.used)} / {_format_bytes(mem.total)} "
            f"({getattr(mem, 'percent', 0):.1f}%)"
        )

    disk_path = _disk_target()
    disk = _safe_call(psutil.disk_usage, None, disk_path)
    if disk is None:
        disk_str = f"N/A (`{disk_path}`)"
    else:
        disk_str = (
            f"{_format_bytes(disk.used)} / {_format_bytes(disk.total)} "
            f"({getattr(disk, 'percent', 0):.1f}%) on `{disk_path}`"
        )

    boot_time = _safe_call(psutil.boot_time)
    if boot_time is None:
        uptime_str = "N/A"
    else:
        uptime = timedelta(seconds=max(0, time.time() - boot_time))
        days = uptime.days
        hours, remainder = divmod(uptime.seconds, 3600)
        minutes = remainder // 60
        uptime_str = f"{days}d {hours}h {minutes}m"

    net = _safe_call(psutil.net_io_counters)
    if net is None:
        network_str = "N/A"
    else:
        network_str = (
            f"↑ {_format_bytes(net.bytes_sent)} / ↓ {_format_bytes(net.bytes_recv)}"
        )

    process = _safe_call(psutil.Process)
    memory_info = _safe_call(process.memory_info) if process is not None else None
    bot_memory = _format_bytes(getattr(memory_info, "rss", None))

    os_name = platform.system() or "Unknown OS"
    os_release = platform.release()
    architecture = platform.machine() or "unknown architecture"
    platform_str = f"{os_name} {os_release} ({architecture})".strip()

    return (
        "**System Stats**\n"
        f"🖥️ Platform: {platform_str}\n"
        f"🌡️ Temperature: {_temperature_string()}\n"
        f"⚙️ CPU: {cpu_percent_str} @ {freq_str} | Cores: {core_str}\n"
        f"📈 Load (1/5/15m): {load_str}\n"
        f"🧠 RAM: {memory_str}\n"
        f"💾 Disk: {disk_str}\n"
        f"🌐 Network: {network_str}\n"
        f"⏱️ Host uptime: {uptime_str}\n"
        f"🤖 Bot memory: {bot_memory}"
    )


class StatsFeature:
    """The /stats command — portable host and bot-process metrics."""

    def __init__(self, client: discord.Client, tree: app_commands.CommandTree):
        self.client = client
        self.tree = tree
        self._register_commands()

    def _register_commands(self) -> None:
        @self.tree.command(
            name="stats",
            description="Show stats for the machine running the bot",
        )
        async def stats(interaction: discord.Interaction):
            logger.info(f"Command /stats called by {interaction.user}")
            await interaction.response.defer()
            try:
                text = await asyncio.to_thread(_collect_stats)
            except Exception:
                logger.exception("Unexpected error while collecting /stats")
                await interaction.followup.send(
                    "Could not collect system stats on this host. Check the bot log."
                )
                return
            await interaction.followup.send(text)
