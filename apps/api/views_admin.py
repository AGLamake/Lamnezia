"""The administrator's page of API tokens: the list, a new token shown once, disabling one."""

from typing import Any

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views import View

from apps.accounts.permissions import AdminRequiredMixin
from apps.api.forms import TokenForm
from apps.api.models import ApiToken
from apps.api.services.tokens import ApiTokens, IssuedToken
from apps.audit.services import journal as audit


class TokenListView(AdminRequiredMixin, View):
    template_name = "api/tokens.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._page(TokenForm())

    def post(self, request: HttpRequest) -> HttpResponse:
        form = TokenForm(request.POST)
        if not form.is_valid():
            return self._page(form)
        issued = ApiTokens().issue(
            name=form.cleaned_data["name"],
            owner=form.cleaned_data["owner"],
            by=self.member,
            scope=form.cleaned_data["scope"],
            expires_at=form.cleaned_data["expires_at"],
        )
        audit.log_request(
            request,
            "api.token_create",
            issued.token,
            owner=issued.token.owner.username,
            scope=issued.token.scope,
        )
        # The value is in this answer only: no redirect, and the browser must not keep it.
        response = self._page(TokenForm(), issued)
        response["Cache-Control"] = "no-store"
        return response

    def _page(self, form: TokenForm, issued: IssuedToken | None = None) -> HttpResponse:
        context: dict[str, Any] = {
            "form": form,
            "issued": issued,
            "tokens": ApiToken.objects.select_related("owner"),
        }
        return render(self.request, self.template_name, context)


class TokenDeactivateView(AdminRequiredMixin, View):
    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        token = get_object_or_404(ApiToken, pk=pk)
        if ApiTokens().deactivate(token):
            audit.log_request(request, "api.token_deactivate", token)
            messages.success(request, _("The token is disabled."))
        return redirect("api-tokens:list")
