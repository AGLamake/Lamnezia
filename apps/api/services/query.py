"""Parameters of API calls: pages of a list, filters by id or by a choice, the body of a change."""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from django.db.models import Model, QuerySet
from django.http import QueryDict

from apps.api.errors import BadBodyError, BadParameterError

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def id_param(query: QueryDict, name: str) -> int | None:
    """A positive id, or None when the parameter is not given."""
    raw = query.get(name, "")
    if not raw:
        return None
    if not raw.isdigit() or int(raw) < 1:
        raise BadParameterError(name, "a positive integer")
    return int(raw)


def choice_param(query: QueryDict, name: str, choices: Iterable[str]) -> str | None:
    raw = query.get(name, "")
    allowed = list(choices)
    if raw and raw not in allowed:
        raise BadParameterError(name, "one of " + ", ".join(allowed))
    return raw or None


@dataclass(frozen=True)
class Page:
    """`limit` items from `offset`; the answer has `count`, the size of the whole list."""

    limit: int
    offset: int

    @classmethod
    def of(cls, query: QueryDict) -> "Page":
        limit = _number(query, "limit", DEFAULT_LIMIT)
        if not 1 <= limit <= MAX_LIMIT:
            raise BadParameterError("limit", f"from 1 to {MAX_LIMIT}")
        return cls(limit, _number(query, "offset", 0))

    def items[M: Model](self, rows: QuerySet[M]) -> tuple[int, list[M]]:
        return rows.count(), list(rows[self.offset : self.offset + self.limit])

    def answer(self, count: int, results: list[dict[str, Any]]) -> dict[str, Any]:
        return {"count": count, "limit": self.limit, "offset": self.offset, "results": results}


def _number(query: QueryDict, name: str, default: int) -> int:
    raw = query.get(name, "")
    if not raw:
        return default
    if not raw.isdigit():
        raise BadParameterError(name, "a non-negative integer")
    return int(raw)


class Body:
    """The JSON object a write call sends, and its fields checked one by one."""

    def __init__(self, raw: bytes) -> None:
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            raise BadBodyError from None
        if not isinstance(data, dict):
            raise BadBodyError
        self._data: dict[str, Any] = data

    def integer(self, name: str, low: int = 1, high: int | None = None) -> int:
        value = self._data.get(name)
        if type(value) is not int or value < low or (high is not None and value > high):
            limits = f"from {low} to {high}" if high is not None else f"at least {low}"
            raise BadParameterError(name, f"an integer {limits}")
        return value

    def text(self, name: str, max_length: int) -> str:
        value = self._data.get(name)
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_length:
            raise BadParameterError(name, f"a string of 1 to {max_length} characters")
        return value.strip()
