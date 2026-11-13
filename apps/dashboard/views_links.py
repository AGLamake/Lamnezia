"""Messenger accounts on the user card: linking by ID, unlinking and link codes for the user."""

from typing import Any

from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _
from django.views import View

from apps.accounts.models import Messenger
from apps.accounts.services.linking import AccountLinks, LinkError, invitation, links_by_code
from apps.audit.services import journal as audit
from apps.core.services.site_settings import cached_settings
from apps.dashboard.forms import LinkAccountForm
from apps.dashboard.views_users import UserDetailView, ZoneUsersMixin


def _messenger(value: str) -> Messenger:
    if value not in Messenger.values:
        raise Http404
    return Messenger(value)


class UserLinkView(ZoneUsersMixin, View):
    """Links the messenger account with the ID the staff member typed in."""

    def post(self, request: HttpRequest, pk: int, messenger: str) -> HttpResponse:
        user, kind = get_object_or_404(self.zone_users(), pk=pk), _messenger(messenger)
        form = LinkAccountForm(request.POST)
        if not form.is_valid():
            for errors in form.errors.values():
                messages.error(request, str(errors[0]))
            return redirect("dashboard:user", pk=user.pk)
        data = form.cleaned_data
        try:
            AccountLinks().link(user, kind, data["external_id"], data["username"])
        except LinkError as error:
            messages.error(request, str(error))
        else:
            audit.log_request(request, "accounts.link", user, messenger=kind)
            messages.success(request, _("Linked."))
        return redirect("dashboard:user", pk=user.pk)


class UserUnlinkView(ZoneUsersMixin, View):
    def post(self, request: HttpRequest, pk: int, messenger: str) -> HttpResponse:
        user, kind = get_object_or_404(self.zone_users(), pk=pk), _messenger(messenger)
        if AccountLinks().unlink(user, kind):
            audit.log_request(request, "accounts.unlink", user, messenger=kind)
            messages.success(request, _("Unlinked."))
        return redirect("dashboard:user", pk=user.pk)


class UserLinkCodeView(UserDetailView):
    """The card again, with a single-use link or bot command to pass on to the user."""

    http_method_names = ["post"]  # noqa: RUF012

    def post(self, request: HttpRequest, pk: int, messenger: str) -> HttpResponse:
        site, kind = cached_settings.get(), _messenger(messenger)
        if not links_by_code(site, kind):
            raise Http404
        user = get_object_or_404(self.zone_users(), pk=pk)
        code = AccountLinks().issue_code(user, kind)
        audit.log_request(request, "accounts.link_code", user, messenger=kind)
        context: dict[str, Any] = self.get_context_data(
            invited=kind,
            invitation=invitation(site, kind, code),
            code_minutes=settings.LINK_CODE_MINUTES,
        )
        return self.render_to_response(context)
