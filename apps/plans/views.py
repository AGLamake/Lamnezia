from typing import Any

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, ProtectedError, QuerySet
from django.forms import ModelForm
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import CreateView, ListView, UpdateView

from apps.accounts.models import User
from apps.accounts.permissions import AdminRequiredMixin, StaffRequiredMixin
from apps.accounts.services.zone import Zone
from apps.audit.services import journal as audit
from apps.plans.forms import PlanForm, UserLimitsForm, UserLookupForm
from apps.plans.models import Plan, UserLimits
from apps.plans.services.changes import apply_speed_if_changed, limits_changed
from apps.plans.services.limits import EffectiveLimits


class PlanListView(AdminRequiredMixin, ListView):  # type: ignore[type-arg]
    template_name = "plans/list.html"
    context_object_name = "plans"

    def get_queryset(self) -> QuerySet[Plan]:
        return Plan.objects.annotate(users=Count("subscriptions"))

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(lookup_form=UserLookupForm(), **kwargs)


class PlanCreateView(AdminRequiredMixin, CreateView):  # type: ignore[type-arg]
    model = Plan
    form_class = PlanForm
    template_name = "plans/form.html"
    success_url = reverse_lazy("plans:list")

    def form_valid(self, form: "ModelForm[Plan]") -> HttpResponse:
        response = super().form_valid(form)
        audit.log_request(self.request, "plans.create", form.instance)
        return response


class PlanUpdateView(AdminRequiredMixin, UpdateView):  # type: ignore[type-arg]
    model = Plan
    form_class = PlanForm
    template_name = "plans/form.html"
    context_object_name = "plan"
    success_url = reverse_lazy("plans:list")

    def form_valid(self, form: "ModelForm[Plan]") -> HttpResponse:
        response = super().form_valid(form)
        audit.log_request(self.request, "plans.update", form.instance, changed=form.changed_data)
        apply_speed_if_changed(form.changed_data)
        return response


class PlanDeleteView(AdminRequiredMixin, View):
    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        plan = get_object_or_404(Plan, pk=pk)
        try:
            with transaction.atomic():
                audit.log_request(request, "plans.delete", plan)
                plan.delete()
        except ProtectedError as error:
            messages.error(request, self._still_used(error))
            return redirect("plans:edit", pk=plan.pk)
        messages.success(request, _("The plan is deleted."))
        return redirect("plans:list")

    @staticmethod
    def _still_used(error: ProtectedError) -> str:
        if any(isinstance(item, UserLimits) for item in error.protected_objects):
            return _("Users still have this plan. Give them another one first.")
        return _(
            "The plan was paid for and stays for the record. Clear its price to stop selling it."
        )


class UserLookupView(AdminRequiredMixin, View):
    """Find a user by name and open their plan and limits."""

    def get(self, request: HttpRequest) -> HttpResponse:
        form = UserLookupForm(request.GET)
        user = None
        if form.is_valid():
            user = User.objects.filter(username__iexact=form.cleaned_data["username"]).first()
        if user is None:
            messages.error(request, _("No user with this name."))
            return redirect("plans:list")
        return redirect("plans:user", pk=user.pk)


class UserLimitsView(StaffRequiredMixin, UpdateView):  # type: ignore[type-arg]
    """A user's plan and overrides, with the limits that result from them; a moderator's own
    users only."""

    form_class = UserLimitsForm
    template_name = "plans/user_limits.html"
    context_object_name = "limits"

    def get_object(self, queryset: QuerySet[UserLimits] | None = None) -> UserLimits:
        user = get_object_or_404(Zone(self.member).users(), pk=self.kwargs["pk"])
        limits, _created = UserLimits.objects.get_or_create(user=user)
        return limits

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        limits: UserLimits = self.object
        return super().get_context_data(effective=EffectiveLimits.of(limits), **kwargs)

    def form_valid(self, form: "ModelForm[UserLimits]") -> HttpResponse:
        limits = form.save()
        limits_changed(limits, form.changed_data, self.member, self.client_ip)
        messages.success(self.request, _("The limits are saved."))
        return HttpResponseRedirect(reverse("plans:user", args=[limits.user.pk]))
