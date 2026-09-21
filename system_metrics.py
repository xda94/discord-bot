"""Shared host-metric helpers used by the Discord bot and dashboard API."""

from typing import Optional

import psutil


_PREFERRED_TEMPERATURE_GROUPS = (
    "cpu_thermal",
    "coretemp",
    "k10temp",
    "zenpower",
    "acpitz",
)


def get_temperature_celsius() -> Optional[float]:
    """Return the first sane CPU temperature reading, when available."""
    sensor_func = getattr(psutil, "sensors_temperatures", None)
    if sensor_func is None:
        return None
    try:
        temperatures = sensor_func() or {}
    except (
        AttributeError,
        FileNotFoundError,
        NotImplementedError,
        OSError,
        RuntimeError,
        ValueError,
    ):
        return None

    ordered_groups = []
    for name in _PREFERRED_TEMPERATURE_GROUPS:
        if name in temperatures:
            ordered_groups.append(temperatures[name])
    ordered_groups.extend(
        entries
        for name, entries in temperatures.items()
        if name not in _PREFERRED_TEMPERATURE_GROUPS
    )
    for entries in ordered_groups:
        for reading in entries or ():
            current = getattr(reading, "current", None)
            if isinstance(current, (int, float)) and -50 <= current <= 200:
                return float(current)
    return None
