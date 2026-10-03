"""Modern, local-only Vega-Lite rendering for wishlist price charts."""

from __future__ import annotations

import json
from datetime import datetime

import vl_convert as vlc
from i18n import t


BACKGROUND = "#111827"
GRID = "#334155"
TEXT = "#F8FAFC"
MUTED_TEXT = "#CBD5E1"
PRIMARY = "#60A5FA"
TARGET = "#FBBF24"


def _time_encoding(timestamps: list[datetime], language="en") -> dict:
    span = max(timestamps) - min(timestamps)
    # Include hours for short histories and years when the range crosses one.
    date_format = (
        "%d %b %H:%M" if span.total_seconds() <= 2 * 86400
        else "%d %b %Y" if min(timestamps).year != max(timestamps).year
        else "%d %b"
    )
    if language == "ro":
        date_format = "%d.%m %H:%M" if span.total_seconds() <= 2 * 86400 else "%d.%m.%Y"
    return {
        "field": "checked_at", "type": "temporal", "scale": {"type": "utc"},
        "axis": {"title": t("chart_time", language), "format": date_format,
                 "labelOverlap": "greedy", "tickCount": 6},
    }


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
    locale = {"decimal": ",", "thousands": ".", "grouping": [3], "currency": ["", ""]} if spec.get("usermeta", {}).get("language") == "ro" else None
    return vlc.vegalite_to_png(vl_spec=json.dumps(spec), scale=2, format_locale=locale)


def render_price_history_png(
    timestamps: list[datetime],
    prices: list[float],
    title: str,
    currency: str,
    target_price: float | None = None,
    *, language: str = "en",
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

    if language == "ro":
        subtitle = [t("chart_observations", language, count=len(prices)),
                    t("chart_summary", language, current=f"{current:.2f}".replace(".", ","),
                      minimum=f"{minimum:.2f}".replace(".", ","), change=f"{change:+.1f}".replace(".", ","), currency=currency)]
    temporal = _time_encoding(timestamps, language)
    quantitative = {
        "field": "price",
        "type": "quantitative",
        "axis": {"title": t("chart_price", language, currency=currency), "format": ".2f"},
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
                "interpolate": "linear",
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
                        "text": {"value": t("chart_target", language, price=f"{target_price:.2f}".replace(".", ",") if language == "ro" else f"{target_price:.2f}", currency=currency)},
                    },
                },
            ]
        )

    spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "usermeta": {"language": language},
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
    percentage: bool = False,
    *, language: str = "en",
) -> bytes:
    values = []
    endpoints = []
    for index, (label, points) in enumerate(series, 1):
        short_label = f"{label[:37]}…" if len(label) > 38 else label
        # Identity must never depend on a (possibly duplicate/truncated) title.
        series_label = f"{index}. {short_label}"
        baseline = float(points[0][1])
        if percentage and baseline <= 0:
            raise ValueError("Percentage comparison requires a positive starting price")
        def plotted_price(price):
            return float(price) / baseline - 1 if percentage else float(price)
        for timestamp, price in points:
            values.append(
                {
                    "item": series_label,
                    "checked_at": timestamp.isoformat(),
                    "price": plotted_price(price),
                }
            )
        last_timestamp, last_price = points[-1]
        endpoints.append(
            {
                "item": series_label,
                "checked_at": last_timestamp.isoformat(),
                "price": plotted_price(last_price),
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
    x = _time_encoding([timestamp for _, points in series for timestamp, _ in points], language)
    y = {
        "field": "price",
        "type": "quantitative",
        "axis": {
            "title": t("chart_change", language) if percentage else t("chart_price", language, currency=currency),
            "format": "+.1%" if percentage else ".2f",
        },
        "scale": {"zero": False, "nice": True},
    }
    spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "usermeta": {"language": language},
        "width": 980,
        "height": 540,
        "padding": 24,
        "title": {
            "text": t("chart_title", language),
            "subtitle": [
                t("chart_products", language, count=len(series)),
                t("chart_start", language) if percentage
                else t("chart_currency", language, currency=currency),
            ],
        },
        "data": {"values": values},
        "layer": [
            {
                "mark": {
                    "type": "line",
                    "strokeWidth": 3,
                    "interpolate": "linear",
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
