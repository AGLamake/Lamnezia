"""Panel versions X.Y.Z: the running one and comparison with the tag vX.Y.Z of a release."""

import re
from dataclasses import dataclass
from functools import cache
from typing import Self

from apps.backups.services.panel_archive import panel_version

_VERSION = re.compile(r"v?(\d{1,6})\.(\d{1,6})\.(\d{1,6})")


@dataclass(frozen=True, order=True)
class Version:
    """A release version; pre-releases (1.1.0-rc.1) are not versions the panel updates to."""

    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, text: str) -> Self | None:
        match = _VERSION.fullmatch(text.strip())
        if match is None:
            return None
        major, minor, patch = (int(part) for part in match.groups())
        return cls(major, minor, patch)

    @property
    def tag(self) -> str:
        return f"v{self}"

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@cache
def running_version() -> Version | None:
    """The version of the running panel; None for a build between releases (1.1.0.dev1)."""
    return Version.parse(panel_version())
