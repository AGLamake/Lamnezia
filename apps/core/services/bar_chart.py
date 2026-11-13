"""Stacked bar chart: the geometry of a server-drawn SVG chart of byte counts per period."""

from collections.abc import Sequence
from dataclasses import dataclass

# Steps of the value axis: 1, 2 and 5 of a power of 1024, so the ticks read as round sizes.
_MULTIPLIERS = (1, 2, 5, 10, 20, 50, 100, 200, 500)
_TICKS = 4
# Share of a column's slot taken by the bar; the rest is the gap between bars.
_BAR_SHARE = 0.7
# A tooltip of a column this close (in percent) to an edge of the plot opens away from it.
_TIP_EDGE = 20


@dataclass(frozen=True)
class Segment:
    """One series of a column, in percent of the plot height from the top."""

    series: int
    top: float
    height: float


@dataclass(frozen=True)
class Column:
    """A bucket: its label, values per series, the bar and its slot in percent of the plot."""

    label: str
    values: tuple[int, ...]
    left: float
    width: float
    segments: tuple[Segment, ...]
    slot_left: float = 0.0
    slot_width: float = 0.0

    @property
    def total(self) -> int:
        return sum(self.values)

    @property
    def tip_side(self) -> str:
        """Where the tooltip opens so it stays inside the plot: `start`, `centre` or `end`."""
        centre = self.slot_left + self.slot_width / 2
        if centre < _TIP_EDGE:
            return "start"
        if centre > 100 - _TIP_EDGE:
            return "end"
        return "centre"


@dataclass(frozen=True)
class Tick:
    """A value axis line: its value and its height in percent from the bottom."""

    value: int
    bottom: float


@dataclass(frozen=True)
class StackedBars:
    """Buckets labelled `labels`; `series[i][j]` is the value of series i in bucket j.

    Positions are percentages, so the SVG stretches to any width while its text stays in HTML.
    """

    labels: Sequence[str]
    series: Sequence[Sequence[int]]

    @property
    def top(self) -> int:
        """The upper end of the value axis: a round step times the number of ticks."""
        return self._step() * _TICKS

    def columns(self) -> list[Column]:
        count = max(len(self.labels), 1)
        slot = 100 / count
        top = self.top
        columns = []
        for index, label in enumerate(self.labels):
            values = tuple(values[index] for values in self.series)
            columns.append(
                Column(
                    label=label,
                    values=values,
                    left=slot * index + slot * (1 - _BAR_SHARE) / 2,
                    width=slot * _BAR_SHARE,
                    segments=self._segments(values, top),
                    slot_left=slot * index,
                    slot_width=slot,
                )
            )
        return columns

    def ticks(self) -> list[Tick]:
        step = self._step()
        return [Tick(step * number, 100 * number / _TICKS) for number in range(_TICKS + 1)]

    def axis_labels(self, most: int = 7) -> list[tuple[str, float]]:
        """At most `most` evenly spread bucket labels with their centre in percent.

        The first and the last bucket are always labelled: the last one is usually "now".
        """
        count = len(self.labels)
        if not count:
            return []
        shown = min(count, max(most, 2))
        if shown == 1:
            indices = [0]
        else:
            indices = sorted({round(n * (count - 1) / (shown - 1)) for n in range(shown)})
        slot = 100 / count
        return [(self.labels[i], slot * (i + 0.5)) for i in indices]

    @property
    def empty(self) -> bool:
        return not any(any(values) for values in self.series)

    @staticmethod
    def _segments(values: tuple[int, ...], top: int) -> tuple[Segment, ...]:
        segments = []
        filled = 0.0
        for series, value in enumerate(values):
            if not value:
                continue
            height = 100 * value / top
            filled += height
            segments.append(Segment(series, 100 - filled, height))
        return tuple(segments)

    def _step(self) -> int:
        highest = max((sum(column) for column in zip(*self.series, strict=True)), default=0)
        unit = 1
        while True:
            for multiplier in _MULTIPLIERS:
                if multiplier * unit * _TICKS >= highest:
                    return multiplier * unit
            unit *= 1024
