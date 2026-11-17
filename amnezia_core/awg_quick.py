"""Why `awg-quick up` refused a config, read from its output without the values it echoes.

`awg setconf` prints a config line it does not know whole, `HeaderProtectionKey` with its key
among them (docs/research/awg-3.1.md, checked on a server in P14-03):

    Line unrecognized: `HeaderProtectionKey=<key>'
    Configuration parsing error

An image older than AmneziaWG 3.1 says that of every 3.x parameter. S1-S4 below 12 next to
HeaderProtectionKey are refused by the image and the kernel module alike:

    Unable to modify interface: Invalid argument
"""

import re
from dataclasses import dataclass
from enum import StrEnum

_UNRECOGNIZED = re.compile(r"^(?P<lead>.*Line unrecognized: `)(?P<line>.*?)('|$)")
_INVALID_ARGUMENT = "Unable to modify interface: Invalid argument"


class UpRefusal(StrEnum):
    UNKNOWN_PARAMETER = "unknown_parameter"
    INVALID_ARGUMENT = "invalid_argument"
    OTHER = "other"


@dataclass(frozen=True)
class UpOutput:
    """What a failed `awg-quick up` printed: the reason and the lines safe to log.

    An unknown config line keeps only its parameter name: the value may be a key.
    """

    refusal: UpRefusal
    lines: tuple[str, ...]

    @classmethod
    def parse(cls, text: str) -> "UpOutput":
        refusal = UpRefusal.OTHER
        lines = []
        for line in filter(None, map(str.strip, text.splitlines())):
            match = _UNRECOGNIZED.match(line)
            if match:
                refusal = UpRefusal.UNKNOWN_PARAMETER
                name = match["line"].split("=", 1)[0].strip()
                lines.append(f"{match['lead']}{name}=…'")
                continue
            if _INVALID_ARGUMENT in line and refusal is UpRefusal.OTHER:
                refusal = UpRefusal.INVALID_ARGUMENT
            lines.append(line)
        return cls(refusal, tuple(lines))
