"""The Bots block of Settings → Integrations: the state of each bot and its buttons."""

from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.models import Role
from apps.accounts.permissions import role_required
from apps.audit.services import journal as audit
from apps.bots.models import BotRuntime
from apps.bots.services.control import BotControl

_control = BotControl()


def panel_context() -> dict[str, object]:
    return {"bot_cards": _control.cards(), "bots_poll_seconds": settings.BOT_SUPERVISOR_SECONDS}


@role_required(Role.ADMIN)
@require_GET
def status(request: HttpRequest) -> HttpResponse:
    return render(request, "bots/_panel.html", panel_context())


@role_required(Role.ADMIN)
@require_POST
def turn(request: HttpRequest, messenger: str) -> HttpResponse:
    runtime = _runtime(messenger)
    on = request.POST.get("on") == "1"
    _control.turn(runtime, on=on)
    audit.log_request(request, "bots.turn_on" if on else "bots.turn_off", runtime)
    messages.success(request, _("The bot is turning on.") if on else _("The bot is turning off."))
    return _back()


@role_required(Role.ADMIN)
@require_POST
def restart(request: HttpRequest, messenger: str) -> HttpResponse:
    runtime = _runtime(messenger)
    _control.restart(runtime)
    audit.log_request(request, "bots.restart", runtime)
    messages.success(request, _("The bot restarts in a few seconds."))
    return _back()


@role_required(Role.ADMIN)
@require_POST
def leave(request: HttpRequest, messenger: str, server_id: str) -> HttpResponse:
    runtime = _runtime(messenger)
    server = runtime.server(server_id)
    if server is None:
        raise Http404
    _control.leave(runtime, server_id)
    audit.log_request(request, "bots.leave", runtime, server=server_id, name=server.get("name"))
    messages.success(request, _("The bot leaves the server in a few seconds."))
    return _back()


def _runtime(messenger: str) -> BotRuntime:
    return get_object_or_404(BotRuntime, messenger=messenger)


def _back() -> HttpResponse:
    return redirect("core:settings")
