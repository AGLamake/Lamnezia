from apps.core.services.sparkline import Sparkline


def test_series_fills_the_box_from_left_to_right() -> None:
    chart = Sparkline([0, 50, 100], width=104, height=34, floor=0, ceiling=100)

    assert chart.segments() == ["2.0,32.0 52.0,17.0 102.0,2.0"]
    assert chart.last_point() == (102.0, 2.0)


def test_missing_values_break_the_line() -> None:
    chart = Sparkline([1, 2, None, 3], floor=0, ceiling=3)

    assert len(chart.segments()) == 2


def test_flat_series_is_drawn_in_the_middle() -> None:
    chart = Sparkline([5, 5, 5], height=34)

    assert {point.split(",")[1] for point in chart.segments()[0].split()} == {"17.0"}


def test_scale_without_bounds_follows_the_data() -> None:
    chart = Sparkline([10, 20], width=104, height=34)

    assert chart.segments() == ["2.0,32.0 102.0,2.0"]


def test_empty_series_draws_nothing() -> None:
    chart = Sparkline([None, None])

    assert chart.segments() == []
    assert chart.last_point() is None
