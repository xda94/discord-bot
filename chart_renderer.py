"""Modern, local-only Vega-Lite rendering for wishlist price charts."""

from __future__ import annotations

import json
from datetime import datetime

import vl_convert as vlc


BACKGROUND = "#111827"
GRID = "#334155"
TEXT = "#F8FAFC"
MUTED_TEXT = "#CBD5E1"
PRIMARY = "#60A5FA"
TARGET = "#FBBF24"


def _base_config() -> dict:
    return {
        "background": BACKGROUND,
        "config": {
            "view": {"stroke": None},
            "axis": {
                "domainColor": GRID,
                "gridColor": GRID,
                "gridOpacity": 0.45,
                "labelColor": MUTED_TEXT,
                "labelFont": "sans-serif",
                "labelFontSize": 13,
                "labelPadding": 8,
                "tickColor": GRID,
                "titleColor": MUTED_TEXT,
                "titleFont": "sans-serif",
                "titleFontSize": 14,
                "titlePadding": 14,
            },
            "legend": {
                "labelColor": MUTED_TEXT,
                "labelFont": "sans-serif",
                "labelFontSize": 12,
                "title": None,
            },
            "title": {
                "anchor": "start",
                "color": TEXT,
                "font": "sans-serif",
                "fontSize": 24,
                "fontWeight": 700,
                "subtitleColor": MUTED_TEXT,
                "subtitleFont": "sans-serif",
                "subtitleFontSize": 13,
                "subtitlePadding": 10,
            },
        },
    }


def _png(spec: dict) -> bytes:
    """Render a self-contained spec; no URLs or external data are accepted."""
    return vlc.vegalite_to_png(vl_spec=json.dumps(spec), scale=2)


def render_price_history_png(
    timestamps: list[datetime],
    prices: list[float],
    title: str,
    currency: str,
    target_price: float | None = None,
) -> bytes:
    first = float(prices[0])
    current = float(prices[-1])
    minimum = min(float(price) for price in prices)
    maximum = max(float(price) for price in prices)
    baseline = minimum - max((maximum - minimum) * 0.18, abs(minimum) * 0.02, 1.0)
    values = [
        {
            "checked_at": timestamp.isoformat(),
            "price": float(price),
            "baseline": baseline,
        }
        for timestamp, price in zip(timestamps, prices)
    ]
    change = ((current / first) - 1) * 100 if first else 0.0
    subtitle = [
        f"{len(prices)} observation{'s' if len(prices) != 1 else ''}",
        f"Current {current:.2f} {currency}  •  Lowest {minimum:.2f} {currency}  •  Change {change:+.1f}%",
    ]

    temporal = {
        "field": "checked_at",
        "type": "temporal",
        "axis": {
            "title": None,
            "format": "%d %b",
            "labelOverlap": "greedy",
            "tickCount": 8,
        },
    }
    quantitative = {
        "field": "price",
        "type": "quantitative",
        "axis": {"title": f"Price ({currency})", "format": ".2f"},
        "scale": {"zero": False, "nice": True},
    }
    layers = [
        {
            "mark": {
                "type": "area",
                "line": False,
                "color": PRIMARY,
                "opacity": 0.12,
            },
            "encoding": {
                "x": temporal,
                "y": quantitative,
                "y2": {"field": "baseline", "type": "quantitative"},
            },
        },
        {
            "mark": {
                "type": "line",
                "color": PRIMARY,
                "strokeWidth": 3,
                "interpolate": "monotone",
                "point": {"filled": True, "fill": BACKGROUND, "size": 65},
            },
            "encoding": {"x": temporal, "y": quantitative},
        },
        {
            "data": {"values": [values[-1]]},
            "mark": {
                "type": "point",
                "filled": True,
                "color": TEXT,
                "stroke": PRIMARY,
                "strokeWidth": 3,
                "size": 180,
            },
            "encoding": {"x": temporal, "y": quantitative},
        },
    ]
    if target_price is not None:
        target_data = [{"target": float(target_price)}]
        layers.extend(
            [
                {
                    "data": {"values": target_data},
                    "mark": {
                        "type": "rule",
                        "color": TARGET,
                        "strokeDash": [8, 6],
                        "strokeWidth": 2,
                    },
                    "encoding": {
                        "y": {
                            "field": "target",
                            "type": "quantitative",
                            "scale": {"zero": False, "nice": True},
                        }
                    },
                },
                {
                    "data": {"values": target_data},
                    "mark": {
                        "type": "text",
                        "align": "right",
                        "baseline": "bottom",
                        "dx": -8,
                        "dy": -5,
                        "color": TARGET,
                        "fontSize": 13,
                        "fontWeight": 600,
                    },
                    "encoding": {
                        "y": {
                            "field": "target",
                            "type": "quantitative",
                            "scale": {"zero": False, "nice": True},
                        },
                        "x": {"value": "width"},
                        "text": {"value": f"Target {target_price:.2f} {currency}"},
                    },
                },
            ]
        )

    spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": 900,
        "height": 480,
        "padding": 24,
        "title": {"text": title[:80], "subtitle": subtitle},
        "data": {"values": values},
        "layer": layers,
        **_base_config(),
    }
    return _png(spec)


def render_multi_price_history_png(
    series: list[tuple[str, list[tuple[datetime, float]]]],
    currency: str,
) -> bytes:
    values = []
    endpoints = []
    for label, points in series:
        short_label = f"{label[:37]}…" if len(label) > 38 else label
        for timestamp, price in points:
            values.append(
                {
                    "item": short_label,
                    "checked_at": timestamp.isoformat(),
                    "price": float(price),
                }
            )
        last_timestamp, last_price = points[-1]
        endpoints.append(
            {
                "item": short_label,
                "checked_at": last_timestamp.isoformat(),
                "price": float(last_price),
            }
        )

    color = {
        "field": "item",
        "type": "nominal",
        "legend": {
            "orient": "bottom",
            "direction": "horizontal",
            "columns": 2,
            "symbolStrokeWidth": 4,
        },
    }
    x = {
        "field": "checked_at",
        "type": "temporal",
        "axis": {
            "title": None,
            "format": "%d %b",
            "labelOverlap": "greedy",
            "tickCount": 8,
        },
    }
    y = {
        "field": "price",
        "type": "quantitative",
        "axis": {"title": f"Price ({currency})", "format": ".2f"},
        "scale": {"zero": False, "nice": True},
    }
    spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": 980,
        "height": 540,
        "padding": 24,
        "title": {
            "text": "Price evolution — all tracked items",
            "subtitle": [
                f"{len(series)} product{'s' if len(series) != 1 else ''}",
                f"All prices converted to {currency}",
            ],
        },
        "data": {"values": values},
        "layer": [
            {
                "mark": {
                    "type": "line",
                    "strokeWidth": 3,
                    "interpolate": "monotone",
                    "point": {"filled": True, "size": 45},
                },
                "encoding": {"x": x, "y": y, "color": color},
            },
            {
                "data": {"values": endpoints},
                "mark": {"type": "point", "filled": True, "size": 130},
                "encoding": {"x": x, "y": y, "color": color},
            },
        ],
        **_base_config(),
    }
    return _png(spec)
