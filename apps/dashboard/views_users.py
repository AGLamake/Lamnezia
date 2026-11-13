"""Users for the staff: the list with filters, the card of a user and bulk actions.

A moderator sees and changes only the users of their zone and their own bulk actions.
"""

from typing import Any

from django.conf import settings
from django.contrib import messages
from django.db.models import QuerySet
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import DetailView, ListView, TemplateView

from apps.accounts.models import User
from apps.accounts.permissions import StaffRequiredMixin
from apps.accounts.services.zone import Zone
from apps.audit.services import journal as audit
from apps.core.paging import PageSizeMixin
from apps.dashboard.forms import UserBatchForm, UserFilterForm
from apps.dashboard.models import BatchAction, UserBatch
from apps.dashboard.services.batch import BatchReport, UserBatchActions
from apps.dashboard.services.users import UserCard, UserRow, users_for_list
from apps.keys.services.subscription import MySubscription
from apps.plans.models import Plan


class ZoneUsersMixin(StaffRequiredMixin):
    def zone_users(self) -> QuerySet[User]:
        return Zone(self.member).users()


class UserListView(PageSizeMixin, ZoneUsersMixin, ListView):  # type: ignore[type-arg]
    template_name = "dashboard/users/list.html"

    def get_queryset(self) -> QuerySet[User]:
        self.filter_form = UserFilterForm(self.request.GET or None)
        return self.filter_form.filter(users_for_list().filter(pk__in=self.zone_users()))

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        return {
            **context,
            "filter_form": self.filter_form,
            "rows": UserRow.rows(context["object_list"]),
            "actions": BatchAction.choices,
            "plans": Plan.objects.all(),
            "filters": self.request.GET.urlencode(),
        }


class UserDetailView(ZoneUsersMixin, TemplateView):
    template_name = "dashboard/users/detail.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        user = get_object_or_404(self.zone_users(), pk=self.kwargs["pk"])
        subscription = MySubscription(user)
        token = subscription.token() if subscription.available else None
        return super().get_context_data(
            card=UserCard.load(user, timezone.now()),
            offers_subscription=subscription.available,
            subscription_url=self.request.build_absolute_uri(
                reverse("subscription-feed", args=[token])
            )
            if token
            else None,
            **kwargs,
        )


class UserSubscriptionView(ZoneUsersMixin, View):
    """A new subscription link for the user, or none: the previous one stops working at once."""

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        user = get_object_or_404(self.zone_users(), pk=pk)
        subscription = MySubscription(user)
        if not subscription.available:
            raise Http404
        if request.POST.get("action") == "delete":
            subscription.delete()
            audit.log_request(request, "keys.subscription_delete", user)
            messages.success(request, _("The subscription link is deleted."))
        else:
            subscription.renew()
            audit.log_request(request, "keys.subscription_renew", user)
            messages.success(request, _("A new subscription link is ready."))
        return redirect("dashboard:user", pk=user.pk)


class UserBatchView(ZoneUsersMixin, View):
    """Starts a bulk action and opens its report; a mistake in the form goes back to the list."""

    def post(self, request: HttpRequest) -> HttpResponse:
        form = UserBatchForm(request.POST, users=self.zone_users())
        if not form.is_valid():
            for errors in form.errors.values():
                messages.error(request, str(errors[0]))
            filters = request.POST.get("filters", "")
            return redirect(
                f"{reverse('dashboard:users')}?{filters}" if filters else "dashboard:users"
            )
        batch = self._start(form.cleaned_data)
        return redirect("dashboard:batch", pk=batch.pk)

    def _start(self, data: dict[str, Any]) -> UserBatch:
        actions = UserBatchActions(self.member, ip=self.client_ip)
        users = data["users"].order_by("username")
        if data["action"] == BatchAction.EXTEND:
            return actions.extend(users, data["days"])
        if data["action"] == BatchAction.CHANGE_PLAN:
            return actions.change_plan(users, data["plan"])
        if data["action"] == BatchAction.REISSUE:
            return actions.reissue(users)
        return actions.disable(users)


class BatchDetailView(StaffRequiredMixin, DetailView):  # type: ignore[type-arg]
    """The report reloads itself over HTMX while key operations are still running."""

    template_name = "dashboard/users/batch.html"

    def get_queryset(self) -> QuerySet[UserBatch]:
        batches = UserBatch.objects.all()
        return batches if self.member.is_admin else batches.filter(created_by=self.member)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(
            report=BatchReport.of(self.object),
            poll_seconds=settings.PROGRESS_POLL_SECONDS,
            **kwargs,
        )


class BatchProgressView(BatchDetailView):
    template_name = "dashboard/users/_report.html"
