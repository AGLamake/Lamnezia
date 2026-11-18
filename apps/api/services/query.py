"""Query parameters of API calls: pages of a list and the filters by id or by a choice."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from django.db.models import Model, QuerySet
from django.http import QueryDict

from apps.api.errors import BadParameterError

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
