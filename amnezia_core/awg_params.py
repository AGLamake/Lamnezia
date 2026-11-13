"""Checks of the AmneziaWG 2.0 and 3.1 settings an administrator enters, before awg0.conf.

Follows upstream AwgProtocolConfig::isHeadersEqual and isPacketSizeEqual and the limits of
amneziawg-go: every packet with its junk must fit into 1280 bytes, and with HeaderProtectionKey
each of S1-S4 is at least 12. An empty value is allowed
everywhere: the parameter is then left out of the file, as the Amnezia app does it.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise

from amnezia_core.awg import SPECIAL_JUNK
from amnezia_core.errors import InvalidKeyError
from amnezia_core.keys import decode_key
from amnezia_core.script_vars import TOGGLES

MAX_PORT = 65535

# Largest value of the size parameters (amneziawg-go limits for a 1280-byte packet).
_SIZE_LIMITS = {
    "Jc": 128,
    "Jmin": 1280,
    "Jmax": 1280,
    "S1": 1280 - 148,
    "S2": 1280 - 92,
    "S3": 1280 - 64,
    "S4": 1280 - 32,
}
# WireGuard message sizes the S1-S4 junk is added to (upstream AwgConstant).
MESSAGE_SIZES = {"S1": 148, "S2": 92, "S3": 64, "S4": 32}
# Smallest S1-S4 with HeaderProtectionKey: its nonce is the first 12 bytes of that padding.
MIN_PADDING = 12
_HEADERS = ("H1", "H2", "H3", "H4")
_MAX_HEADER = 2**32 - 1
_RANGE_PARAMS = (
    "ContentPaddingAddition",
    "RekeyAfterTime",
    "RekeyTimeout",
    "RejectAfterTime",
    "KeepaliveTimeout",
    "MaxHandshakeAttempts",
)
_TOGGLE_VALUES = ("on", "off")

_NUMBER = re.compile(r"\d+")
_RANGE = re.compile(r"(?P<start>\d+)(?:-(?P<end>\d+))?")
# Tags of the AmneziaWG special junk packets: bytes, random bytes/chars/digits, counter, time.
_JUNK_TAGS = re.compile(r"(?:<(?:b 0x[0-9a-fA-F]+|r \d+|rc \d+|rd \d+|c|t)>)+")


class Problem(StrEnum):
    NOT_A_NUMBER = "not_a_number"
    TOO_LARGE = "too_large"
    NOT_A_RANGE = "not_a_range"
    MIN_ABOVE_MAX = "min_above_max"
    HEADERS_OVERLAP = "headers_overlap"
    SIZES_COLLIDE = "sizes_collide"
    NOT_A_KEY = "not_a_key"
    NOT_A_TOGGLE = "not_a_toggle"
    NOT_JUNK_TAGS = "not_junk_tags"
    PADDING_TOO_SMALL = "padding_too_small"


@dataclass(frozen=True)
class ParamProblem:
    """What is wrong with one parameter; the interface turns `problem` into a message."""

    param: str
    problem: Problem


def check_awg_params(params: Mapping[str, str]) -> list[ParamProblem]:
    """Every problem of the parameters, at most one per parameter; empty if they can be saved."""
    values = {name: value.strip() for name, value in params.items()}
    problems = [*_check_sizes(values), *_check_formats(values)]
    seen = {problem.param for problem in problems}
    problems += [p for p in _check_relations(values) if p.param not in seen]
    return problems


def _check_sizes(values: Mapping[str, str]) -> list[ParamProblem]:
    problems = []
    for name, limit in _SIZE_LIMITS.items():
        value = values.get(name, "")
        if value and not _NUMBER.fullmatch(value):
            problems.append(ParamProblem(name, Problem.NOT_A_NUMBER))
        elif value and int(value) > limit:
            problems.append(ParamProblem(name, Problem.TOO_LARGE))
    return problems


def _check_formats(values: Mapping[str, str]) -> list[ParamProblem]:
    return [
        ParamProblem(name, problem)
        for names, is_valid, problem in _FORMATS
        for name in names
        if values.get(name) and not is_valid(values[name])
    ]


def _check_relations(values: Mapping[str, str]) -> list[ParamProblem]:
    problems = []
    if _number(values, "Jmin") > _number(values, "Jmax", default=_SIZE_LIMITS["Jmax"]):
        problems.append(ParamProblem("Jmin", Problem.MIN_ABOVE_MAX))
    if _headers_overlap(values):
        problems.append(ParamProblem("H1", Problem.HEADERS_OVERLAP))
    if values.get("HeaderProtectionKey"):
        problems += [
            ParamProblem(name, Problem.PADDING_TOO_SMALL)
            for name in MESSAGE_SIZES
            if _number(values, name) < MIN_PADDING
        ]
    sizes = [_number(values, name) + size for name, size in MESSAGE_SIZES.items()]
    if len(set(sizes)) != len(sizes):
        problems.append(ParamProblem("S1", Problem.SIZES_COLLIDE))
    return problems


def _headers_overlap(values: Mapping[str, str]) -> bool:
    """H1-H4 must be different (upstream isHeadersEqual); AWG 2.0 ranges must not intersect."""
    ranges = sorted(_bounds(values[name]) for name in _HEADERS if _is_range(values.get(name, "")))
    return any(next_start <= end for (_, end), (next_start, _) in pairwise(ranges))


def _is_range(value: str) -> bool:
    if _RANGE.fullmatch(value) is None:
        return False
    start, end = _bounds(value)
    return start <= end <= _MAX_HEADER


def _bounds(value: str) -> tuple[int, int]:
    match = _RANGE.fullmatch(value)
    if match is None:
        return 0, 0
    return int(match["start"]), int(match["end"] or match["start"])


def _is_toggle(value: str) -> bool:
    return value.lower() in _TOGGLE_VALUES


def _is_key(value: str) -> bool:
    try:
        decode_key(value)
    except InvalidKeyError:
        return False
    return True


def _is_junk_tags(value: str) -> bool:
    return _JUNK_TAGS.fullmatch(value) is not None


_FORMATS: tuple[tuple[tuple[str, ...], Callable[[str], bool], Problem], ...] = (
    ((*_HEADERS, *_RANGE_PARAMS), _is_range, Problem.NOT_A_RANGE),
    (TOGGLES, _is_toggle, Problem.NOT_A_TOGGLE),
    (SPECIAL_JUNK, _is_junk_tags, Problem.NOT_JUNK_TAGS),
    (("HeaderProtectionKey",), _is_key, Problem.NOT_A_KEY),
)


def _number(values: Mapping[str, str], name: str, default: int = 0) -> int:
    value = values.get(name, "")
    return int(value) if _NUMBER.fullmatch(value) else default
