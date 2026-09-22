from datetime import datetime, timedelta

import pytest

import wishlist.charts as chart_renderer
from wishlist.charts import render_multi_price_history_png, render_price_history_png


def _sample_points():
    start = datetime(2026, 1, 1)
    timestamps = [start + timedelta(days=index * 7) for index in range(4)]
    prices = [500.0, 480.0, 490.0, 450.0]
    return timestamps, prices


def test_single_price_chart_renders_png_with_target():
    timestamps, prices = _sample_points()

    image = render_price_history_png(
        timestamps, prices, "Test product", "RON", target_price=460.0
    )

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(image) > 10_000


def test_duplicate_titles_stay_separate_and_percentages_use_each_baseline(monkeypatch):
    timestamps, _ = _sample_points()
    captured = []
    monkeypatch.setattr(chart_renderer, "_png", lambda spec: captured.append(spec))
    long_title = "Identical product name " * 4
    render_multi_price_history_png([
        (long_title + "A", [(timestamps[0], 100), (timestamps[1], 80)]),
        (long_title + "B", [(timestamps[0], 500), (timestamps[1], 550)]),
        (long_title + "B", [(timestamps[0], 200), (timestamps[1], 210)]),
    ], "RON", percentage=True)
    spec = captured[0]
    rows = spec["data"]["values"]
    assert len({row["item"] for row in rows}) == 3
    assert [row["price"] for row in rows] == pytest.approx([0, -.2, 0, .1, 0, .05])
    assert [row["price"] for row in spec["layer"][1]["data"]["values"]] == pytest.approx([-.2, .1, .05])
    assert spec["layer"][0]["mark"]["interpolate"] == "linear"


def test_short_and_cross_year_history_have_unambiguous_date_labels(monkeypatch):
    captured = []
    monkeypatch.setattr(chart_renderer, "_png", lambda spec: captured.append(spec))
    start = datetime(2026, 1, 1)
    for end in [start + timedelta(hours=2), start + timedelta(days=400)]:
        render_price_history_png([start, end], [10, 12], "Item", "RON")
    short_x = captured[0]["layer"][1]["encoding"]["x"]
    long_x = captured[1]["layer"][1]["encoding"]["x"]
    assert "%H:%M" in short_x["axis"]["format"]
    assert "%Y" in long_x["axis"]["format"]
    assert short_x["scale"]["type"] == "utc"
    assert all(spec["layer"][1]["mark"]["interpolate"] == "linear" for spec in captured)


def test_percentage_chart_renders_with_single_point_and_negative_change():
    timestamps, prices = _sample_points()
    png = render_multi_price_history_png([
        ("Same name", list(zip(timestamps, prices))),
        ("Same name", [(timestamps[0], 200)]),
    ], "RON", percentage=True)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_multi_price_chart_renders_png():
    timestamps, prices = _sample_points()
    second = [price / 2 for price in prices]

    image = render_multi_price_history_png(
        [
            ("First product", list(zip(timestamps, prices))),
            ("Second product", list(zip(timestamps, second))),
        ],
        "RON",
    )

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(image) > 10_000
