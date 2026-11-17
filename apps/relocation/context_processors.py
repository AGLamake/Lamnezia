"""Template context of the panel move: the banner of a read-only panel."""

from typing import Any

from django.http import HttpRequest

from apps.relocation.services.freeze import frozen_move


def panel_move(request: HttpRequest) -> dict[str, Any]:
    return {"frozen_move": frozen_move()}
