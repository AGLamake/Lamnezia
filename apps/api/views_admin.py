"""The page of API tokens: a staff member's own tokens, a new one shown once, disabling one.

Each staff member issues tokens for themselves, with permissions within their zone. The
administrator also sees and disables everyone's tokens.
"""

from typing import Any, cast

from django.contrib import messages
from django.db.models import QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views import View

from apps.accounts.permissions import StaffRequiredMixin
from apps.api.forms import BasePermissionFormSet, PermissionFormSet, TokenForm
from apps.api.models import ApiToken
from apps.api.services.tokens import ApiTokens, IssuedToken, PermissionZoneError
from apps.audit.services import journal as audit


class _TokensMixin(StaffRequiredMixin):
    def tokens(self) -> QuerySet[ApiToken]:
        tokens = ApiToken.objects.select_related("owner").prefetch_related(
            "permissions__server", "permissions__user", "permissions__key"
        )
        return tokens if self.member.is_admin else tokens.filter(owner=self.member)


class TokenListView(_TokensMixin, View):
    template_name = "api/tokens.html"

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._page(TokenForm(), self._permissions())

    def post(self, request: HttpRequest) -> HttpResponse:
        form, permissions = TokenForm(request.POST), self._permissions(request.POST)
        if not (form.is_valid() and permissions.is_valid()):
            return self._page(form, permissions)
        try:
            issued = ApiTokens().issue(
                name=form.cleaned_data["name"],
                owner=self.member,
                by=self.member,
                permissions=permissions.permissions(),
                expires_at=form.cleaned_data["expires_at"],
            )
        except PermissionZoneError:
            # The zone shrank between the page and the press: offer the rows again.
            form.add_error(None, _("An object is no longer in your zone. Check the permissions."))
            return self._page(form, self._permissions(request.POST))
        audit.log_request(
            request,
            "api.token_create",
            issued.token,
            owner=issued.token.owner.username,
            permissions=len(permissions.permissions()),
        )
        # The value is in this answer only: no redirect, and the browser must not keep it.
        response = self._page(TokenForm(), self._permissions(), issued)
        response["Cache-Control"] = "no-store"
        return response

    def _permissions(self, data: Any = None) -> BasePermissionFormSet:
        formset = PermissionFormSet(data, prefix="permissions", form_kwargs={"member": self.member})
        return cast(BasePermissionFormSet, formset)

    def _page(
        self, form: TokenForm, permissions: BasePermissionFormSet, issued: IssuedToken | None = None
    ) -> HttpResponse:
        context: dict[str, Any] = {
            "form": form,
            "permissions": permissions,
            "issued": issued,
            "tokens": self.tokens(),
        }
        return render(self.request, self.template_name, context)


class TokenDeactivateView(_TokensMixin, View):
    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        token = get_object_or_404(self.tokens(), pk=pk)
        if ApiTokens().deactivate(token):
            audit.log_request(request, "api.token_deactivate", token)
            messages.success(request, _("The token is disabled."))
        return redirect("api-tokens:list")
