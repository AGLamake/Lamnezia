"""Registration for the staff: the queue of new users and invites.

The queue is for administrators. A moderator makes and deletes their own invites, whose users
join their zone.
"""

from typing import Any, cast

from django.contrib import messages
from django.db.models import QuerySet
from django.forms import ModelForm
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import CreateView, TemplateView

from apps.accounts.forms import ApproveForm, InviteForm
from apps.accounts.models import Invite, Status, User
from apps.accounts.permissions import AdminRequiredMixin, StaffRequiredMixin
from apps.accounts.services.applications import Applications
from apps.audit.services import journal as audit
from apps.core.services.site_settings import cached_settings


def _applications(request: HttpRequest) -> Applications:
    return Applications(cast(User, request.user), ip=audit.client_ip(request))


def _invites(member: User) -> QuerySet[Invite]:
    """Every invite for an administrator; a moderator's own for a moderator."""
    invites = Invite.objects.select_related("plan", "created_by")
    return invites if member.is_admin else invites.filter(created_by=member)


class RegistrationsView(StaffRequiredMixin, TemplateView):
    template_name = "accounts/registrations.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        invites = [
            (
                invite,
                self.request.build_absolute_uri(reverse("accounts:invite", args=[invite.code])),
            )
            for invite in _invites(self.member)
        ]
        if self.member.is_admin:
            kwargs["waiting"] = Applications.waiting()
            kwargs["approve_form"] = ApproveForm(
                initial={"plan": cached_settings.get().default_plan_id}, auto_id=False
            )
        return super().get_context_data(invites=invites, **kwargs)


class ApproveView(AdminRequiredMixin, View):
    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        user = get_object_or_404(Applications.waiting(), pk=pk)
        form = ApproveForm(request.POST)
        if form.is_valid():
            _applications(request).approve(user, form.cleaned_data["plan"])
            messages.success(request, _("%(user)s can use the panel now.") % {"user": user})
        else:
            messages.error(request, _("Choose a plan."))
        return redirect("accounts:registrations")


class RejectView(AdminRequiredMixin, View):
    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        user = get_object_or_404(User, pk=pk, status=Status.PENDING)
        _applications(request).reject(user)
        messages.success(request, _("%(user)s is blocked.") % {"user": user})
        return redirect("accounts:registrations")


class InviteCreateView(StaffRequiredMixin, CreateView):  # type: ignore[type-arg]
    form_class = InviteForm
    template_name = "accounts/invite_form.html"
    success_url = reverse_lazy("accounts:registrations")

    def form_valid(self, form: "ModelForm[Invite]") -> HttpResponse:
        form.instance.created_by = self.member
        response = super().form_valid(form)
        audit.log_request(
            self.request,
            "accounts.invite_create",
            form.instance,
            plan_id=form.instance.plan_id,
            max_uses=form.instance.max_uses,
        )
        return response


class InviteDeleteView(StaffRequiredMixin, View):
    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        invite = get_object_or_404(_invites(self.member), pk=pk)
        audit.log_request(request, "accounts.invite_delete", invite)
        invite.delete()
        messages.success(request, _("The invite is deleted."))
        return redirect("accounts:registrations")
