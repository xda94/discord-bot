from datetime import datetime, timedelta

import pytest

import wishlist.charts as chart_renderer
from wishlist.charts import MAX_COMBINED_PRODUCTS, render_multi_price_history_png, render_price_history_png


def _sample_points():
    start = datetime(2026, 1, 1)
    timestamps = [start + timedelta(days=index * 7) for index in range(4)]
    prices = [500.0, 480.0, 490.0, 450.0]
    return timestamps, prices


def _capture(monkeypatch):
    captured = []
    monkeypatch.setattr(chart_renderer, "_png", lambda spec: captured.append(spec) or b"png")
    return captured


def _nodes(node):
    """Walk every nested view of a spec."""
    if isinstance(node, dict):
        yield node
        node = list(node.values())
    if isinstance(node, list):
        for child in node:
            yield from _nodes(child)


def _texts(spec):
    return [
        node["text"]["value"] for node in _nodes(spec)
        if isinstance(node.get("text"), dict) and "value" in node["text"]
    ]


def _marks(spec, mark_type):
    return [
        node for node in _nodes(spec)
        if isinstance(node.get("mark"), dict) and node["mark"].get("type") == mark_type
    ]


def _board_rows(spec):
    return next(mark for mark in _marks(spec, "circle"))["encoding"], next(
        node["data"]["values"] for node in _nodes(spec)
        if node.get("width") == 300 and "data" in node
    )


def _x_axis_format(spec):
    line = _marks(spec, "line")[0]
    return line["encoding"]["x"]


def test_single_price_chart_renders_png_with_target():
    timestamps, prices = _sample_points()

    image = render_price_history_png(
        timestamps, prices, "Test product", "RON", target_price=460.0
    )

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(image) > 10_000


@pytest.mark.parametrize("prices", [[100.0], [100.0, 100.0], [0.0, 100.0]])
def test_single_chart_renders_single_flat_and_zero_start_histories(prices):
    start = datetime(2026, 1, 1)
    timestamps = [start + timedelta(hours=index) for index in range(len(prices))]
    assert render_price_history_png(timestamps, prices, "Item", "RON").startswith(b"\x89PNG")


def test_single_chart_has_buy_zone_median_and_low_only_when_meaningful(monkeypatch):
    specs = _capture(monkeypatch)
    timestamps, prices = _sample_points()
    render_price_history_png(timestamps, prices, "Item", "RON", target_price=460.0)
    render_price_history_png(timestamps, prices, "Item", "RON")
    render_price_history_png(timestamps[:2], [100.0, 100.0], "Item", "RON")

    with_target = _texts(specs[0])
    assert "450 RON" in with_target
    assert "Buy zone · target 460" in with_target
    assert "Median 485" in with_target
    assert "Lowest 450" in with_target
    assert "−7.2% vs median" in with_target
    assert "In your buy zone · −10.0% since first check" in with_target
    assert len(_marks(specs[0], "rect")) == 1

    assert len(_marks(specs[1], "rect")) == 0
    assert "−10.0% since first check" in _texts(specs[1])
    assert not any(text.startswith("Lowest") for text in _texts(specs[2]))


def test_history_is_drawn_as_steps(monkeypatch):
    specs = _capture(monkeypatch)
    timestamps, prices = _sample_points()
    points = list(zip(timestamps, prices))
    render_price_history_png(timestamps, prices, "Item", "RON")
    render_multi_price_history_png([("Item", points)], "RON")
    assert all(mark["mark"]["interpolate"] == "step-after" for spec in specs for mark in _marks(spec, "line"))


def test_grid_orders_biggest_drops_first_and_keeps_duplicate_titles_apart(monkeypatch):
    specs = _capture(monkeypatch)
    timestamps, _ = _sample_points()
    long_title = "Identical product name " * 4
    render_multi_price_history_png([
        ("Rising", [(timestamps[0], 100), (timestamps[1], 150)]),
        (long_title + "A", [(timestamps[0], 100), (timestamps[1], 80)]),
        (long_title + "B", [(timestamps[0], 100), (timestamps[1], 70)]),
    ], "RON")
    texts = _texts(specs[0])
    labels = [text for text in texts if text.startswith(("Rising", "Identical"))]
    assert labels[-1] == "Rising"
    assert labels[0] != labels[1]
    assert texts.index("−30.0%") < texts.index("−20.0%") < texts.index("+50.0%")
    assert "70 RON" in texts


def test_combined_views_cap_products_and_say_so(monkeypatch):
    specs = _capture(monkeypatch)
    timestamps, _ = _sample_points()
    total = MAX_COMBINED_PRODUCTS + 3
    series = [
        (f"Item {index}", [(timestamps[0], 100), (timestamps[1], 100 - index)])
        for index in range(total)
    ]
    render_multi_price_history_png(series, "RON")
    render_multi_price_history_png(series, "RON", percentage=True)

    grid, board = specs
    assert len(_marks(grid, "line")) == MAX_COMBINED_PRODUCTS
    assert "Item 14" in _texts(grid) and "Item 0" not in _texts(grid)
    assert any(f"{MAX_COMBINED_PRODUCTS} of {total} products" in text for text in _texts(grid))
    _, rows = _board_rows(board)
    assert len(rows) == MAX_COMBINED_PRODUCTS
    assert any(f"{MAX_COMBINED_PRODUCTS} of {total} products" in text for text in _texts(board))


def test_board_ranks_by_median_and_uses_each_products_own_baseline(monkeypatch):
    specs = _capture(monkeypatch)
    timestamps, _ = _sample_points()
    long_title = "Identical product name " * 4
    render_multi_price_history_png([
        (long_title + "A", [(timestamps[0], 100), (timestamps[1], 100), (timestamps[2], 110)]),
        (long_title + "B", [(timestamps[0], 500), (timestamps[1], 500), (timestamps[2], 400)]),
    ], "RON", percentage=True)
    _, rows = _board_rows(specs[0])
    assert [row["today"] for row in rows] == pytest.approx([-.2, .1])
    assert [row["low"] for row in rows] == pytest.approx([-.2, 0])
    assert [row["high"] for row in rows] == pytest.approx([0, .1])
    assert [row["versus"] for row in rows] == ["−20.0%", "+10.0%"]
    assert [row["since_start"] for row in rows] == ["−20.0%", "+10.0%"]
    assert len({row["label"] for row in rows}) == 2


def test_board_rejects_nonpositive_baselines():
    start = datetime(2026, 1, 1)
    with pytest.raises(ValueError):
        render_multi_price_history_png([("Zero", [(start, 0)])], "RON", percentage=True)


def test_short_and_cross_year_history_have_unambiguous_date_labels(monkeypatch):
    specs = _capture(monkeypatch)
    start = datetime(2026, 1, 1)
    for end in [start + timedelta(hours=2), start + timedelta(days=400)]:
        render_price_history_png([start, end], [10, 12], "Item", "RON")
    short_x, long_x = (_x_axis_format(spec) for spec in specs)
    assert "%H:%M" in short_x["axis"]["format"]
    assert "%Y" in long_x["axis"]["format"]
    assert short_x["scale"]["type"] == "utc"


def test_romanian_numbers_use_local_separators(monkeypatch):
    specs = _capture(monkeypatch)
    start = datetime(2026, 1, 1)
    render_price_history_png(
        [start, start + timedelta(days=1)], [2000.5, 1948.25], "Item", "RON", language="ro"
    )
    texts = _texts(specs[0])
    assert "1.948,25 RON" in texts
    assert "−2,6% de la prima verificare" in texts


@pytest.mark.parametrize("language", ["en", "ro"])
def test_comparison_chart_renders_with_single_point_and_negative_change(language):
    timestamps, prices = _sample_points()
    png = render_multi_price_history_png([
        ("Same name", list(zip(timestamps, prices))),
        ("Same name", [(timestamps[0], 200)]),
    ], "RON", percentage=True, language=language)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_comparison_chart_renders_when_every_price_is_flat():
    start = datetime(2026, 1, 1)
    png = render_multi_price_history_png([("Flat", [(start, 100)])], "RON", percentage=True)
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
