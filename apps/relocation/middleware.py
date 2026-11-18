"""Answers changes with 503 while the panel moves to another machine; reading still works."""

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import Resolver404, resolve

from apps.relocation.services.freeze import frozen_move

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Signing in, the language and the move's own buttons work on a frozen panel.
ALLOWED_VIEWS = frozenset(
    {
        "accounts:login",
        "accounts:logout",
        "accounts:two-factor-verify",
        "set_language",
        "relocation:confirm",
        "relocation:cancel",
        "relocation:abandon",
        "relocation:auto-stop",
    }
)
# Payment providers and clients retry a 503; by then the domain points to the new machine.
RETRY_AFTER_SECONDS = 300


class PanelFreezeMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.method in SAFE_METHODS or _allowed(request.path_info):
            return self.get_response(request)
        move = frozen_move()
        if move is None:
            return self.get_response(request)
        response = render(request, "relocation/frozen.html", {"move": move}, status=503)
        response["Retry-After"] = str(RETRY_AFTER_SECONDS)
        return response


def _allowed(path: str) -> bool:
    try:
        return resolve(path).view_name in ALLOWED_VIEWS
    except Resolver404:
        return False
