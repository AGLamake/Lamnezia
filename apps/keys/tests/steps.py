from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from django.utils.functional import Promise

FIXTURES = Path(__file__).resolve().parents[3] / "amnezia_core" / "tests" / "fixtures"


class RecordedSteps:
    """StepRecorder that remembers step titles and log lines."""

    def __init__(self) -> None:
        self.titles: list[str] = []
        self.lines: list[str] = []

    @contextmanager
    def step(self, title: str | Promise) -> Iterator[None]:
        self.titles.append(str(title))
        yield

    def log(self, line: str) -> None:
        self.lines.append(line)
