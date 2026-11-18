"""Pages of the panel move: start it, follow its steps and the domain, confirm or cancel it."""

from collections.abc import Callable
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.views import View
from django.views.generic import DetailView, FormView

from apps.accounts.models import User
from apps.accounts.permissions import AdminRequiredMixin
from apps.relocation.errors import MoveError
from apps.relocation.forms import MoveForm
from apps.relocation.models import PanelMove
from apps.relocation.services.domain import panel_domain
from apps.relocation.services.moves import (
    abandon_move,
    cancel_move,
    confirm_move,
    move_operations,
    start_move,
)
from apps.relocation.services.panel_machine import PanelMachine
from apps.relocation.services.shutdown import switch_auto_stop


class MoveView(AdminRequiredMixin, FormView):  # type: ignore[type-arg]
    """The latest move with its buttons, and the form to start a new one."""

    template_name = "relocation/move.html"
    form_class = MoveForm

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(move=PanelMove.latest(), **kwargs)

    def get_form_kwargs(self) -> dict[str, Any]:
        return {**super().get_form_kwargs(), "panel": PanelMachine.current(self.request.get_host())}

    def form_valid(self, form: MoveForm) -> HttpResponse:
        try:
            move = start_move(
                form.cleaned_data["target"],
                self.member,
                self.client_ip,
                auto_stop=form.cleaned_data["auto_stop"],
            )
        except MoveError as exc:
            form.add_error(None, f"{exc.message} {exc.hint}")
            return self.form_invalid(form)
        return redirect("relocation:progress", pk=move.pk)


class MoveProgressView(AdminRequiredMixin, DetailView):  # type: ignore[type-arg]
    """The steps of a move, then the wait for the domain; reloads itself over HTMX meanwhile."""

    template_name = "relocation/progress.html"
    context_object_name = "move"
    model = PanelMove
    # The domain is checked once a minute: the page needs no faster reload while it waits.
    DOMAIN_POLL_SECONDS = 15

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        move: PanelMove = self.object
        poll_seconds = settings.PROGRESS_POLL_SECONDS if move.busy else self.DOMAIN_POLL_SECONDS
        return super().get_context_data(
            operations=move_operations(move),
            domain=panel_domain(),
            polling=move.busy or move.awaits_domain or move.should_stop,
            poll_seconds=poll_seconds,
            **kwargs,
        )


class MoveProgressPartView(MoveProgressView):
    template_name = "relocation/_progress.html"


class _MoveActionView(AdminRequiredMixin, View):
    action: Callable[[int, User, str | None], PanelMove]

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        try:
            move = type(self).action(pk, self.member, self.client_ip)
        except (MoveError, PanelMove.DoesNotExist) as exc:
            return _refused(request, exc)
        return redirect("relocation:progress", pk=move.pk)


class MoveConfirmView(_MoveActionView):
    action = staticmethod(confirm_move)


class MoveCancelView(_MoveActionView):
    action = staticmethod(cancel_move)


class MoveAbandonView(_MoveActionView):
    action = staticmethod(abandon_move)


class MoveAutoStopView(AdminRequiredMixin, View):
    """Turns the stop of the old copy on or off; works while the panel is read-only."""

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        enabled = request.POST.get("enabled") == "1"
        try:
            switch_auto_stop(pk, enabled, self.member, self.client_ip)
        except (MoveError, PanelMove.DoesNotExist) as exc:
            return _refused(request, exc)
        return redirect("relocation:progress", pk=pk)


def _refused(request: HttpRequest, exc: MoveError | PanelMove.DoesNotExist) -> HttpResponse:
    text = f"{exc.message} {exc.hint}" if isinstance(exc, MoveError) else str(exc)
    messages.error(request, text)
    return redirect("relocation:move")
