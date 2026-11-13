"""Sparkline: the points of a small inline SVG line chart for a short series of numbers."""

from collections.abc import Sequence
from dataclasses import dataclass

# Room around the line so the stroke and the end dot are not cut off.
_PAD = 2.0


@dataclass(frozen=True)
class Sparkline:
    """A series drawn into a `width` x `height` box; None marks a day without data (a gap).

    A flat series is drawn in the middle; the scale starts at `floor` when it is given, so
    percentages keep their meaning (0-100) instead of stretching small changes.
    """

    values: Sequence[float | None]
    width: int = 120
    height: int = 32
    floor: float | None = None
    ceiling: float | None = None

    @property
    def known(self) -> list[float]:
        return [value for value in self.values if value is not None]

    def segments(self) -> list[str]:
        """SVG `points` of each unbroken run of values."""
        runs: list[list[str]] = [[]]
        for index, value in enumerate(self.values):
            if value is None:
                runs.append([])
            else:
                x, y = self._point(index, value)
                runs[-1].append(f"{x:.1f},{y:.1f}")
        return [" ".join(run) for run in runs if run]

    def last_point(self) -> tuple[float, float] | None:
        for index in range(len(self.values) - 1, -1, -1):
            value = self.values[index]
            if value is not None:
                return self._point(index, value)
        return None

    def _point(self, index: int, value: float) -> tuple[float, float]:
        low, high = self._range()
        steps = max(len(self.values) - 1, 1)
        x = _PAD + (self.width - 2 * _PAD) * index / steps
        share = 0.5 if high == low else (value - low) / (high - low)
        y = self.height - _PAD - (self.height - 2 * _PAD) * share
        return x, y

    def _range(self) -> tuple[float, float]:
        known = self.known
        low = self.floor if self.floor is not None else min(known)
        high = self.ceiling if self.ceiling is not None else max(known)
        return low, max(high, low)
