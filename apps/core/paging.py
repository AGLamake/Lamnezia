"""Page size of the staff lists: 25, 50 or 100 rows, chosen with `?per_page=` and remembered."""

from typing import Any

from django.http import HttpRequest

PAGE_SIZES = (25, 50, 100)
_SESSION_KEY = "page_size"
_PARAM = "per_page"


class PageSize:
    """The size a request asks for; a valid choice is kept in the session for the next lists."""

    def __init__(self, request: HttpRequest) -> None:
        self._request = request

    def resolve(self) -> int:
        asked = self._parse(self._request.GET.get(_PARAM))
        if asked is not None:
            self._request.session[_SESSION_KEY] = asked
            return asked
        return self._parse(self._request.session.get(_SESSION_KEY)) or PAGE_SIZES[0]

    @staticmethod
    def _parse(value: object) -> int | None:
        try:
            size = int(str(value))
        except ValueError:
            return None
        return size if size in PAGE_SIZES else None


class PageSizeMixin:
    """For a `ListView`: the page size comes from `PageSize`; the template gets the choices."""

    request: HttpRequest

    def get_paginate_by(self, queryset: object) -> int:
        return PageSize(self.request).resolve()

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context: dict[str, Any] = super().get_context_data(**kwargs)  # type: ignore[misc]
        context["page_sizes"] = PAGE_SIZES
        return context
