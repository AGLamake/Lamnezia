from apps.core.services.bar_chart import StackedBars


def test_axis_top_is_a_round_size_above_the_highest_column() -> None:
    chart = StackedBars(labels=["a", "b"], series=[[300, 100], [450, 0]])

    assert chart.top == 800
    assert [tick.value for tick in chart.ticks()] == [0, 200, 400, 600, 800]


def test_axis_steps_in_powers_of_1024() -> None:
    chart = StackedBars(labels=["a"], series=[[3 * 1024**3]])

    assert chart.top == 4 * 1024**3


def test_empty_chart_still_has_an_axis() -> None:
    chart = StackedBars(labels=["a", "b"], series=[[0, 0], [0, 0]])

    assert chart.top == 4
    assert all(column.segments == () for column in chart.columns())


def test_segments_stack_from_the_bottom() -> None:
    chart = StackedBars(labels=["a"], series=[[200], [600]])

    column = chart.columns()[0]
    first, second = column.segments
    assert column.total == 800
    assert (first.series, first.top, first.height) == (0, 75.0, 25.0)
    assert (second.series, second.top, second.height) == (1, 0.0, 75.0)


def test_bars_share_the_width_with_gaps() -> None:
    chart = StackedBars(labels=["a", "b"], series=[[1, 2]])

    left, right = chart.columns()
    assert round(left.left, 3) == 7.5
    assert round(left.width, 3) == 35.0
    assert round(right.left, 3) == 57.5


def test_axis_labels_are_thinned_to_the_limit() -> None:
    labels = [str(hour) for hour in range(24)]
    chart = StackedBars(labels=labels, series=[[0] * 24])

    axis = chart.axis_labels()
    assert [text for text, _ in axis] == ["0", "4", "8", "12", "15", "19", "23"]
    assert axis[0][1] == 100 / 24 / 2


def test_a_narrow_axis_keeps_the_first_and_the_last_label() -> None:
    labels = [str(day) for day in range(30)]
    chart = StackedBars(labels=labels, series=[[1] * 30])

    assert [text for text, _ in chart.axis_labels(3)] == ["0", "14", "29"]
    assert not chart.empty


def test_a_chart_of_zeros_is_empty() -> None:
    assert StackedBars(labels=["a", "b"], series=[[0, 0], [0, 0]]).empty


def test_tooltips_of_the_edge_columns_open_inwards() -> None:
    chart = StackedBars(labels=[str(day) for day in range(10)], series=[[1] * 10])

    sides = [column.tip_side for column in chart.columns()]
    assert sides[0] == sides[1] == "start"
    assert sides[5] == "centre"
    assert sides[-1] == sides[-2] == "end"
    assert chart.columns()[3].slot_left == 30.0
