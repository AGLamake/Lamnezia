"""Pages of the panel move: start it, then confirm or cancel it."""

from collections.abc import Callable
from typing import Any

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.views import View
from django.views.generic import FormView

from apps.accounts.models import User
from apps.accounts.permissions import AdminRequiredMixin
from apps.relocation.errors import MoveError
from apps.relocation.forms import MoveForm
from apps.relocation.models import PanelMove
from apps.relocation.services.moves import cancel_move, confirm_move, start_move


class MoveView(AdminRequiredMixin, FormView):  # type: ignore[type-arg]
    """The latest move with its buttons, and the form to start a new one."""

    template_name = "relocation/move.html"
    form_class = MoveForm

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(move=PanelMove.latest(), **kwargs)

    def form_valid(self, form: MoveForm) -> HttpResponse:
        try:
            move = start_move(form.cleaned_data["target"], self.member, self.client_ip)
        except MoveError as exc:
            form.add_error(None, f"{exc.message} {exc.hint}")
            return self.form_invalid(form)
        return redirect("operations:detail", pk=move.operation_id)


class _MoveActionView(AdminRequiredMixin, View):
    action: Callable[[int, User, str | None], PanelMove]

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        try:
            move = type(self).action(pk, self.member, self.client_ip)
        except (MoveError, PanelMove.DoesNotExist) as exc:
            text = f"{exc.message} {exc.hint}" if isinstance(exc, MoveError) else str(exc)
            messages.error(request, text)
            return redirect("relocation:move")
        return redirect("operations:detail", pk=move.operation_id)


class MoveConfirmView(_MoveActionView):
    action = staticmethod(confirm_move)


class MoveCancelView(_MoveActionView):
    action = staticmethod(cancel_move)
