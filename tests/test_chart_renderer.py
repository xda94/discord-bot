from datetime import datetime, timedelta

from chart_renderer import render_multi_price_history_png, render_price_history_png


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
