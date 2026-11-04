from datetime import datetime, timedelta

import pytest

import wishlist.charts as chart_renderer
from wishlist.charts import MAX_COMBINED_PRODUCTS, PRODUCTS_PER_IMAGE, render_multi_price_history_pngs, render_price_history_png


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
        if node.get("width") == chart_renderer.BOARD_LABEL_WIDTH and "data" in node
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
    render_multi_price_history_pngs([("Item", points)], "RON")
    assert all(mark["mark"]["interpolate"] == "step-after" for spec in specs for mark in _marks(spec, "line"))


def test_grid_orders_biggest_drops_first_and_keeps_duplicate_titles_apart(monkeypatch):
    specs = _capture(monkeypatch)
    timestamps, _ = _sample_points()
    long_title = "Identical product name " * 4
    render_multi_price_history_pngs([
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


def _falling_series(count):
    timestamps, _ = _sample_points()
    return [
        (f"Item {index}", [(timestamps[0], 1000), (timestamps[1], 1000 - index)])
        for index in range(count)
    ]


def _corner_texts(points):
    domain = chart_renderer._price_domain([price for _, price in points], chart_renderer.GRID_PLOT_PADDING)
    time_domain = chart_renderer._time_domain([timestamp for timestamp, _ in points])
    return chart_renderer._corner_labels(points, time_domain, domain, "en")


def test_grid_labels_every_corner_of_a_price_drop():
    start = datetime(2026, 1, 1)
    points = [(start + timedelta(days=index), price) for index, price in
              enumerate([315.0] * 100 + [276.98] * 80)]
    assert sorted(label["text"] for label in _corner_texts(points)) == ["276.98", "276.98", "315", "315"]


def test_grid_shows_one_label_when_a_drop_is_too_recent_for_two():
    start = datetime(2026, 1, 1)
    points = [(start + timedelta(days=index), price) for index, price in
              enumerate([315.0] * 170 + [276.98] * 10)]
    assert sorted(label["text"] for label in _corner_texts(points)) == ["276.98", "315", "315"]


def test_todays_price_finds_another_side_when_its_usual_spot_is_blocked():
    start = datetime(2026, 1, 1)
    prices = [200.0] * 80 + [100.0] * 80 + [130.0] * 5 + [115.0] * 15
    points = [(start + timedelta(days=index), price) for index, price in enumerate(prices)]
    assert "115" in [label["text"] for label in _corner_texts(points)]


def test_grid_labels_go_above_a_high_corner_and_below_a_low_one():
    start = datetime(2026, 1, 1)
    points = [(start, 100.0), (start + timedelta(days=50), 100.0),
              (start + timedelta(days=51), 60.0), (start + timedelta(days=100), 60.0)]
    labels = _corner_texts(points)
    high = max(label["y"] for label in labels if label["text"] == "100")
    low = min(label["y"] for label in labels if label["text"] == "60")
    assert high < low
    assert all(0 <= label["y"] <= chart_renderer.GRID_PLOT_HEIGHT - chart_renderer.GRID_LABEL_HEIGHT for label in labels)


def test_grid_labels_stay_inside_and_never_overlap_on_busy_histories():
    start = datetime(2026, 1, 1)
    points = [(start + timedelta(days=index), 1000.0 + (index // 2 % 7) * 37.5) for index in range(90)]
    labels = _corner_texts(points)
    boxes = []
    for label in labels:
        width = chart_renderer._label_width(label["text"], chart_renderer.GRID_LABEL_SIZE)
        assert 0 <= label["x"] <= chart_renderer.GRID_CELL_WIDTH - width
        boxes.append((label["x"], label["y"], label["x"] + width, label["y"] + chart_renderer.GRID_LABEL_HEIGHT))
    assert labels
    for index, box in enumerate(boxes):
        assert not any(chart_renderer._boxes_overlap(box, other) for other in boxes[index + 1:])


def test_grid_labels_a_flat_or_single_observation_history_once_per_end():
    start = datetime(2026, 1, 1)
    assert [label["text"] for label in _corner_texts([(start, 99.0)])] == ["99"]
    flat = [(start + timedelta(days=index), 99.0) for index in range(30)]
    assert [label["text"] for label in _corner_texts(flat)] == ["99", "99"]


def test_grid_corner_labels_reach_the_rendered_spec(monkeypatch):
    specs = _capture(monkeypatch)
    start = datetime(2026, 1, 1)
    points = [(start + timedelta(days=index), price) for index, price in enumerate([315.0] * 60 + [276.98] * 60)]
    render_multi_price_history_pngs([("Billecart", points)], "RON")
    assert _texts(specs[0]).count("315") == 2
    assert "276.98" in _texts(specs[0])


def test_same_prefix_labels_stay_distinct_within_the_length_limit():
    labels = chart_renderer._unique_labels(["Samsung Galaxy S24 128GB", "Samsung Galaxy S24 256GB", "Short"], 12)
    assert labels == ["Samsung Gal…", "Samsung … #2", "Short"]
    assert all(len(label) <= 12 for label in labels)


def test_grid_continues_one_ranking_across_images(monkeypatch):
    specs = _capture(monkeypatch)
    images = render_multi_price_history_pngs(_falling_series(21), "RON")
    assert len(images) == len(specs) == 2
    first, second = specs
    assert len(_marks(first, "line")) == PRODUCTS_PER_IMAGE
    assert len(_marks(second, "line")) == 9
    assert "Item 20" in _texts(first) and "Item 0" in _texts(second)
    assert any("Page 1 of 2" in text for text in _texts(first))
    assert any("Page 2 of 2" in text for text in _texts(second))


def test_board_continues_ranking_and_scale_across_images(monkeypatch):
    specs = _capture(monkeypatch)
    render_multi_price_history_pngs(_falling_series(21), "RON", percentage=True)
    assert len(specs) == 2
    first_rows, second_rows = (_board_rows(spec)[1] for spec in specs)
    assert len(first_rows) == PRODUCTS_PER_IMAGE and len(second_rows) == 9
    assert first_rows[0]["label"] == "Item 20" and second_rows[-1]["label"] == "Item 0"
    domains = [
        next(mark["encoding"]["x"]["scale"]["domain"] for mark in _marks(spec, "circle"))
        for spec in specs
    ]
    assert domains[0] == domains[1]
    assert any("Page 2 of 2" in text for text in _texts(specs[1]))


def test_list_that_fits_one_image_has_no_page_label(monkeypatch):
    specs = _capture(monkeypatch)
    render_multi_price_history_pngs(_falling_series(PRODUCTS_PER_IMAGE), "RON")
    assert len(specs) == 1
    assert not any("Page" in text for text in _texts(specs[0]))


@pytest.mark.parametrize("percentage", [False, True])
def test_products_beyond_the_attachment_limit_are_left_out(monkeypatch, percentage):
    specs = _capture(monkeypatch)
    images = render_multi_price_history_pngs(
        _falling_series(MAX_COMBINED_PRODUCTS + 1), "RON", percentage=percentage,
    )
    assert len(images) == 10


def test_board_ranks_by_median_and_uses_each_products_own_baseline(monkeypatch):
    specs = _capture(monkeypatch)
    timestamps, _ = _sample_points()
    long_title = "Identical product name " * 4
    render_multi_price_history_pngs([
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
        render_multi_price_history_pngs([("Zero", [(start, 0)])], "RON", percentage=True)


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
    png, = render_multi_price_history_pngs([
        ("Same name", list(zip(timestamps, prices))),
        ("Same name", [(timestamps[0], 200)]),
    ], "RON", percentage=True, language=language)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_comparison_chart_renders_when_every_price_is_flat():
    start = datetime(2026, 1, 1)
    png, = render_multi_price_history_pngs([("Flat", [(start, 100)])], "RON", percentage=True)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


def test_multi_price_chart_renders_png():
    timestamps, prices = _sample_points()
    second = [price / 2 for price in prices]

    image, = render_multi_price_history_pngs(
        [
            ("First product", list(zip(timestamps, prices))),
            ("Second product", list(zip(timestamps, second))),
        ],
        "RON",
    )

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(image) > 10_000
