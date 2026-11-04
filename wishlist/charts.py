"""Local-only Vega-Lite rendering for wishlist price charts.

Prices hold between scrapes and jump when a shop changes them, so every history
is drawn as a step line. Three views share one look:

- single item: price history with the median and a target "buy zone"
- all items, prices: a grid of mini charts, biggest drops first
- all items, comparison: a ranked board of today's price against each median

The all-items views span several images when a wishlist doesn't fit in one.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta

import vl_convert as vlc
from i18n import t


SURFACE = "#1a1a19"
TEXT = "#f0efec"
SECONDARY_TEXT = "#c3c2b7"
MUTED_TEXT = "#8f8e85"
GRID = "#383835"
RANGE_BAR = "#4a4a46"
LINE = "#3987e5"
PRICE_DOWN = "#0ca30c"
PRICE_UP = "#ec835a"

WIDTH = 760
GRID_CELL_WIDTH = 356
GRID_PLOT_HEIGHT = 132
GRID_PLOT_PADDING = 24 / 84  # Keeps a label's height free above and below the line.
GRID_LABEL_SIZE = 14
GRID_LABEL_HEIGHT = 16
GRID_LABEL_GAP = 6
BOARD_LABEL_WIDTH = 250
BOARD_LABEL_CHARACTERS = 31
PRODUCTS_PER_IMAGE = 12
MAX_IMAGES = 10  # Discord's attachment limit for one message
MAX_COMBINED_PRODUCTS = PRODUCTS_PER_IMAGE * MAX_IMAGES
PERCENT_AXIS_LABEL = "datum.value === 0 ? '0%' : format(datum.value, '+.0%')"
_FLAT_CHANGE_PERCENT = 0.05


def _money(value: float, language: str) -> str:
    text = f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"
    return text.translate(str.maketrans(",.", ".,")) if language == "ro" else text


def _percent(value: float, language: str) -> str:
    if abs(value) < _FLAT_CHANGE_PERCENT:
        return "0%"
    text = f"{value:+.1f}%".replace("-", "−")
    return text.replace(".", ",") if language == "ro" else text


def _change_percent(first: float, current: float) -> float:
    return (current / first - 1) * 100 if first else 0.0


def _direction_color(change: float) -> str:
    if change <= -_FLAT_CHANGE_PERCENT:
        return PRICE_DOWN
    return PRICE_UP if change >= _FLAT_CHANGE_PERCENT else MUTED_TEXT


def _unique_labels(labels: list[str], length: int) -> list[str]:
    """Shorten titles while keeping identical or same-prefix products distinct."""
    def shorten(label: str, room: int) -> str:
        return f"{label[:room - 1]}…" if len(label) > room else label

    seen: dict[str, int] = {}
    unique = []
    for label in labels:
        short = shorten(label, length)
        seen[short] = seen.get(short, 0) + 1
        if seen[short] == 1:
            unique.append(short)
        else:
            suffix = f" #{seen[short]}"
            unique.append(shorten(label, length - len(suffix)) + suffix)
    return unique


def _row(timestamp: datetime, price: float) -> dict:
    return {"checked_at": timestamp.isoformat(), "price": float(price)}


def _price_domain(values: list[float], padding: float = 0.2) -> list[float]:
    low, high = min(values), max(values)
    span = max(high - low, abs(high) * 0.02, 1.0)
    return [low - span * padding, high + span * padding]


def _time_domain(timestamps: list[datetime]) -> list[str]:
    first, last = min(timestamps), max(timestamps)
    if first == last:
        first, last = first - timedelta(hours=1), last + timedelta(hours=1)
    return [first.isoformat(), last.isoformat()]


def _time_format(timestamps: list[datetime], language: str) -> str:
    span = max(timestamps) - min(timestamps)
    short_span = span.total_seconds() <= 2 * 86400
    if language == "ro":
        return "%d.%m %H:%M" if short_span else "%d.%m.%Y"
    if short_span:
        return "%d %b %H:%M"
    return "%d %b %Y" if min(timestamps).year != max(timestamps).year else "%d %b"


def _time_x(domain: list[str], axis: dict | None = None) -> dict:
    return {
        "field": "checked_at", "type": "temporal",
        "scale": {"type": "utc", "domain": domain},
        "axis": axis,
    }


def _text(x: float, y: float, value: str, size: int, color: str, *, weight: int = 400,
          align: str = "left", baseline: str = "alphabetic", limit: float = 0) -> dict:
    return {
        "mark": {"type": "text", "align": align, "baseline": baseline, "fontSize": size,
                 "fontWeight": weight, "color": color, "limit": limit},
        "encoding": {"x": {"value": x}, "y": {"value": y}, "text": {"value": value}},
    }


def _heading(title: str, note: str, *, width: float = WIDTH, x: float = 0) -> dict:
    return {
        "width": width, "height": 62, "data": {"values": [{}]},
        "layer": [
            _text(x, 24, title, 26, TEXT, weight=700),
            _text(x, 52, note, 16, SECONDARY_TEXT),
        ],
    }


def _document(spec: dict, language: str) -> dict:
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "usermeta": {"language": language},
        "background": SURFACE,
        "padding": 28,
        "config": {
            "view": {"stroke": None},
            "font": "sans-serif",
            "axis": {"labelColor": MUTED_TEXT, "labelFontSize": 15, "labelPadding": 10,
                     "gridColor": GRID, "gridOpacity": 0.7, "domain": False, "ticks": False},
            "concat": {"spacing": 14},
        },
        **spec,
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
    values = [float(price) for price in prices]
    first, current = values[0], values[-1]
    low, high = min(values), max(values)
    median = statistics.median(values)
    in_zone = target_price is not None and current <= target_price

    domain = _price_domain(values + ([float(target_price)] if target_price is not None else []))
    scale = {"domain": domain, "nice": False}
    y = {"field": "price", "type": "quantitative", "scale": scale,
         "axis": {"title": None, "tickCount": 4, "format": ",.2~f"}}
    time_domain = _time_domain(timestamps)
    x = _time_x(time_domain, {
        "title": None, "format": _time_format(timestamps, language), "grid": False,
        "domain": True, "domainColor": GRID, "labelOverlap": "greedy", "tickCount": 6,
    })
    data = [_row(timestamp, price) for timestamp, price in zip(timestamps, values)]

    layers = []
    if target_price is not None:
        layers.append({
            "data": {"values": [{"low": domain[0], "target": float(target_price)}]},
            "mark": {"type": "rect", "color": PRICE_DOWN, "opacity": 0.2},
            "encoding": {
                "y": {"field": "low", "type": "quantitative", "scale": scale, "axis": y["axis"]},
                "y2": {"field": "target"},
            },
        })
    layers.append({
        "data": {"values": [{"price": median}]},
        "mark": {"type": "rule", "color": MUTED_TEXT, "strokeDash": [5, 5], "strokeWidth": 1.5},
        "encoding": {"y": y},
    })
    layers.append({
        "data": {"values": [{"price": median}]},
        "mark": {"type": "text", "align": "left", "baseline": "bottom", "x": 8, "dy": -5,
                 "fontSize": 15, "color": SECONDARY_TEXT},
        "encoding": {"y": y, "text": {"value": t("chart_median", language, price=_money(median, language))}},
    })
    if target_price is not None:
        layers.append({
            "data": {"values": [{"price": float(target_price)}]},
            "mark": {"type": "text", "align": "left", "baseline": "top", "x": 8, "dy": 6,
                     "fontSize": 15, "fontWeight": 600, "color": TEXT},
            "encoding": {"y": y, "text": {"value": t(
                "chart_buy_zone", language, price=_money(float(target_price), language))}},
        })
    layers.append({
        "mark": {"type": "line", "interpolate": "step-after", "color": LINE, "strokeWidth": 2.5},
        "encoding": {"x": x, "y": y},
    })
    if low < high:
        low_index = len(values) - 1 - values[::-1].index(low)
        position = low_index / (len(values) - 1)
        align = "left" if position < 0.15 else "right" if position > 0.85 else "center"
        low_row = [data[low_index]]
        layers.append({
            "data": {"values": low_row},
            "mark": {"type": "point", "filled": True, "size": 90, "color": LINE, "opacity": 1,
                     "stroke": SURFACE, "strokeWidth": 2},
            "encoding": {"x": x, "y": y},
        })
        layers.append({
            "data": {"values": low_row},
            "mark": {"type": "text", "align": align, "baseline": "top", "dy": 10,
                     "fontSize": 15, "color": SECONDARY_TEXT},
            "encoding": {"x": x, "y": y, "text": {"value": t("chart_low", language, price=_money(low, language))}},
        })
    layers.append({
        "data": {"values": [data[-1]]},
        "mark": {"type": "point", "filled": True, "size": 150, "color": TEXT, "opacity": 1,
                 "stroke": LINE, "strokeWidth": 3},
        "encoding": {"x": x, "y": y},
    })

    since_start = t("chart_since_start", language, change=_percent(_change_percent(first, current), language))
    note = f"{t('chart_in_zone', language)} · {since_start}" if in_zone else since_start
    versus = _percent(_change_percent(median, current), language)
    header = {
        "width": WIDTH, "height": 96, "data": {"values": [{}]},
        "layer": [
            _text(0, 18, title, 18, SECONDARY_TEXT, weight=500, limit=WIDTH),
            _text(0, 68, f"{_money(current, language)} {currency}", 44, TEXT, weight=700),
            _text(WIDTH, 40, note, 16, SECONDARY_TEXT, weight=500, align="right"),
            _text(WIDTH, 68, t("chart_vs_median", language, change=versus), 21, TEXT, weight=700, align="right"),
        ],
    }
    plot = {"width": WIDTH, "height": 340, "data": {"values": data}, "layer": layers}
    return _png(_document({"vconcat": [header, plot]}, language))


def _pages(items: list) -> list[list]:
    shown = items[:MAX_COMBINED_PRODUCTS]
    return [shown[index:index + PRODUCTS_PER_IMAGE] for index in range(0, len(shown), PRODUCTS_PER_IMAGE)]


def _page_note(page: int, pages: int, note: str, language: str) -> str:
    if pages > 1:
        return f"{t('chart_page', language, page=page, pages=pages)} · {note}"
    return note


def _label_width(text: str, size: int) -> float:
    """Estimate rendered width; the renderer can't be asked before drawing."""
    units = sum(0.556 if char.isdigit() else 0.278 if char in ",." else 0.6 for char in text)
    return units * size


def _boxes_overlap(first: tuple, second: tuple, gap: float = 0) -> bool:
    return (first[0] < second[2] + gap and second[0] < first[2] + gap
            and first[1] < second[3] + gap and second[1] < first[3] + gap)


def _corner_labels(points: list[tuple[datetime, float]], time_domain: list[str],
                   price_domain: list[float], language: str) -> list[dict]:
    """Place a price at each corner of a step line, skipping any that wouldn't stay legible.

    Today's price, the first price, and the biggest jumps claim space first. A label
    sits on the open side of its corner and tries the other sides if blocked.
    """
    start = datetime.fromisoformat(time_domain[0])
    span = (datetime.fromisoformat(time_domain[1]) - start).total_seconds()
    low, high = price_domain

    def x_at(timestamp: datetime) -> float:
        return GRID_CELL_WIDTH * (timestamp - start).total_seconds() / span

    def y_at(price: float) -> float:
        return GRID_PLOT_HEIGHT * (high - price) / (high - low)

    prices = [float(price) for _, price in points]
    xs = [x_at(timestamp) for timestamp, _ in points]
    ys = [y_at(price) for price in prices]
    path = []
    for index in range(len(points) - 1):
        path.append((xs[index], ys[index], xs[index + 1], ys[index]))
        path.append((xs[index + 1], min(ys[index], ys[index + 1]), xs[index + 1], max(ys[index], ys[index + 1])))

    moves = [index for index in range(1, len(prices)) if prices[index] != prices[index - 1]]
    rose_last = bool(moves) and prices[moves[-1]] > prices[moves[-1] - 1]
    dropped_first = bool(moves) and prices[moves[0]] < prices[moves[0] - 1]
    # (x, y, price, label above the line, preferred side of the corner)
    corners = [(xs[-1], ys[-1], prices[-1], rose_last or not moves, "right")]
    if len(points) > 1:
        corners.append((xs[0], ys[0], prices[0], dropped_first or not moves, "left"))
    for index in sorted(moves, key=lambda move: -abs(prices[move] - prices[move - 1])):
        rose = prices[index] > prices[index - 1]
        corners.append((xs[index], ys[index - 1], prices[index - 1], not rose, "right"))
        corners.append((xs[index], ys[index], prices[index], rose, "left"))

    placed: list[dict] = []
    boxes: list[tuple] = []
    shown_prices: list[tuple[str, float]] = []
    for corner_x, corner_y, price, above, preferred in corners:
        text = _money(price, language)
        width = _label_width(text, GRID_LABEL_SIZE)
        if any(shown == text and abs(shown_x - corner_x) < width * 2 for shown, shown_x in shown_prices):
            continue
        over = corner_y - GRID_LABEL_GAP - GRID_LABEL_HEIGHT
        under = corner_y + GRID_LABEL_GAP
        spots = [
            (top, align)
            for top in ((over, under) if above else (under, over))
            for align in (preferred, "left" if preferred == "right" else "right")
        ]
        for top, align in spots:
            left = corner_x - width if align == "right" else corner_x
            left = min(max(left, 0), GRID_CELL_WIDTH - width)
            box = (left, top, left + width, top + GRID_LABEL_HEIGHT)
            if top < 0 or top + GRID_LABEL_HEIGHT > GRID_PLOT_HEIGHT:
                continue
            if any(_boxes_overlap(box, other, 4) for other in boxes):
                continue
            if any(_boxes_overlap(box, segment) for segment in path):
                continue
            boxes.append(box)
            shown_prices.append((text, corner_x))
            placed.append({"x": left, "y": top, "text": text})
            break
    return placed


def _grid_cell(label: str, points: list[tuple[datetime, float]], currency: str,
               time_domain: list[str], language: str) -> dict:
    current = float(points[-1][1])
    change = _change_percent(float(points[0][1]), current)
    color = _direction_color(change)
    domain = _price_domain([float(price) for _, price in points], GRID_PLOT_PADDING)
    y = {"field": "price", "type": "quantitative", "scale": {"domain": domain, "nice": False}, "axis": None}
    x = _time_x(time_domain)
    corner_labels = [
        {"data": {"values": [{}]},
         **_text(corner["x"], corner["y"], corner["text"], GRID_LABEL_SIZE, SECONDARY_TEXT, baseline="top")}
        for corner in _corner_labels(points, time_domain, domain, language)
    ]
    return {
        "vconcat": [
            {
                "width": GRID_CELL_WIDTH, "height": 58, "data": {"values": [{}]},
                "layer": [
                    _text(0, 16, label, 16, SECONDARY_TEXT, weight=500, limit=GRID_CELL_WIDTH),
                    _text(0, 49, f"{_money(current, language)} {currency}", 25, TEXT, weight=700),
                    _text(GRID_CELL_WIDTH, 49, _percent(change, language), 18, TEXT, weight=700, align="right"),
                ],
            },
            {
                "width": GRID_CELL_WIDTH, "height": GRID_PLOT_HEIGHT,
                "data": {"values": [_row(*point) for point in points]},
                "layer": [
                    {"mark": {"type": "area", "interpolate": "step-after", "color": color, "opacity": 0.13},
                     "encoding": {"x": x, "y": y, "y2": {"datum": domain[0]}}},
                    {"mark": {"type": "line", "interpolate": "step-after", "color": color, "strokeWidth": 2.5},
                     "encoding": {"x": x, "y": y}},
                    {"data": {"values": [_row(*points[-1])]},
                     "mark": {"type": "point", "filled": True, "size": 70, "color": color, "opacity": 1},
                     "encoding": {"x": x, "y": y}},
                    *corner_labels,
                ],
            },
        ],
        "spacing": 6,
    }


def _render_grid(series, currency: str, language: str) -> list[bytes]:
    labels = _unique_labels([label for label, _ in series], 44)
    ranked = sorted(
        zip(labels, (points for _, points in series)),
        key=lambda item: _change_percent(float(item[1][0][1]), float(item[1][-1][1])),
    )
    pages = _pages(ranked)
    time_domain = _time_domain([timestamp for page in pages for _, points in page for timestamp, _ in points])
    images = []
    for number, page in enumerate(pages, 1):
        cells = [_grid_cell(label, points, currency, time_domain, language) for label, points in page]
        rows = [{"hconcat": cells[index:index + 2], "spacing": 28} for index in range(0, len(cells), 2)]
        note = _page_note(number, len(pages), t("chart_grid_note", language), language)
        heading = _heading(t("chart_grid_title", language), note, width=GRID_CELL_WIDTH * 2 + 28)
        images.append(_png(_document({"vconcat": [heading, *rows], "spacing": 24}, language)))
    return images


def _render_board(series, language: str) -> list[bytes]:
    rows = []
    for label, (_, points) in zip(_unique_labels([label for label, _ in series], BOARD_LABEL_CHARACTERS), series):
        prices = [float(price) for _, price in points]
        median = statistics.median(prices)
        if prices[0] <= 0 or median <= 0:
            raise ValueError("Comparison requires positive starting and median prices")
        versus = prices[-1] / median - 1
        rows.append({
            "label": label,
            "low": min(prices) / median - 1,
            "high": max(prices) / median - 1,
            "today": versus,
            "versus": _percent(versus * 100, language),
            "since_start": _percent(_change_percent(prices[0], prices[-1]), language),
            "color": _direction_color(versus * 100),
        })
    rows.sort(key=lambda row: row["today"])
    for rank, row in enumerate(rows):
        row["rank"] = rank
    pages = _pages(rows)
    # One scale across images, so a dot's position means the same on every page.
    limit = max(max(abs(row["low"]), abs(row["high"])) for row in rows) * 1.08 or 0.05
    return [
        _board_image(page, limit, number, len(pages), language)
        for number, page in enumerate(pages, 1)
    ]


def _board_image(rows: list[dict], limit: float, page: int, pages: int, language: str) -> bytes:
    height = len(rows) * 46

    y = {"field": "rank", "type": "ordinal", "axis": None,
         "scale": {"domain": [row["rank"] for row in rows]}}
    x = {"type": "quantitative", "scale": {"domain": [-limit, limit], "nice": False},
         "axis": {"title": None, "labelExpr": PERCENT_AXIS_LABEL, "tickCount": 3, "orient": "top"}}
    range_width = 250

    def column(field: str, header: str, width: int, weight: int, color: str, align: str = "right") -> dict:
        anchor = width if align == "right" else 0
        return {
            "width": width, "height": height, "data": {"values": rows},
            "layer": [
                {"mark": {"type": "text", "align": align, "fontSize": 15, "fontWeight": weight,
                          "color": color, "x": anchor, "limit": width},
                 "encoding": {"y": y, "text": {"field": field}}},
                {"data": {"values": [{}]}, **_text(anchor, -13, header, 14, MUTED_TEXT, align=align)},
            ],
        }

    ranges = {
        "width": range_width, "height": height, "data": {"values": rows},
        "layer": [
            {"data": {"values": [{"middle": 0}]},
             "mark": {"type": "rule", "color": MUTED_TEXT, "strokeWidth": 1.5},
             "encoding": {"x": {"field": "middle", **x}}},
            {"mark": {"type": "rule", "strokeWidth": 9, "color": RANGE_BAR, "strokeCap": "round"},
             "encoding": {"y": y, "x": {"field": "low", **x}, "x2": {"field": "high"}}},
            {"mark": {"type": "circle", "size": 170, "opacity": 1, "stroke": SURFACE, "strokeWidth": 2},
             "encoding": {"y": y, "x": {"field": "today", **x},
                          "color": {"field": "color", "type": "nominal", "scale": None}}},
        ],
    }
    versus_width, since_width, spacing = 96, 104, 8
    note = _page_note(page, pages, t("chart_board_note", language), language)
    heading = _heading(
        t("chart_board_title", language), note,
        width=BOARD_LABEL_WIDTH + range_width + versus_width + since_width + spacing * 3,
    )
    board = {
        "hconcat": [
            column("label", "", BOARD_LABEL_WIDTH, 400, SECONDARY_TEXT, "left"),
            ranges,
            column("versus", t("chart_median_column", language), versus_width, 700, TEXT),
            column("since_start", t("chart_start_column", language), since_width, 400, SECONDARY_TEXT),
        ],
        "spacing": spacing,
    }
    return _png(_document({"vconcat": [heading, board], "spacing": 20}, language))


def render_multi_price_history_pngs(
    series: list[tuple[str, list[tuple[datetime, float]]]],
    currency: str,
    percentage: bool = False,
    *, language: str = "en",
) -> list[bytes]:
    if percentage:
        return _render_board(series, language)
    return _render_grid(series, currency, language)
