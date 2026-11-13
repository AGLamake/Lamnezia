from typing import Any

from django.contrib import messages
from django.db.models import QuerySet
from django.forms import ModelForm
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import CreateView, ListView, UpdateView

from apps.accounts.permissions import AdminRequiredMixin
from apps.audit.services import journal as audit
from apps.core.paging import PageSizeMixin
from apps.notifications.events import routable_events
from apps.notifications.forms import CHANNEL_FORMS, ChannelForm, DeliveryFilterForm
from apps.notifications.models import Channel, ChannelKind, Delivery
from apps.notifications.services.delivery import send_test


class ChannelListView(AdminRequiredMixin, ListView):  # type: ignore[type-arg]
    """Channels and the routing table: which channels hear of each event."""

    template_name = "notifications/channels.html"
    context_object_name = "channels"

    def get_queryset(self) -> QuerySet[Channel]:
        return Channel.objects.prefetch_related("routes")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        channels = list(self.object_list)
        routing = [
            (label, [c for c in channels if any(r.event == event for r in c.routes.all())])
            for event, label in routable_events()
        ]
        return super().get_context_data(routing=routing, kinds=ChannelKind.choices, **kwargs)


_CHANNEL_FORM_TEMPLATE = "notifications/channel_form.html"


class ChannelCreateView(AdminRequiredMixin, CreateView):  # type: ignore[type-arg]
    template_name = _CHANNEL_FORM_TEMPLATE
    success_url = reverse_lazy("notifications:channels")

    def get_form_class(self) -> type[ChannelForm]:
        try:
            return CHANNEL_FORMS[self.kwargs["kind"]]
        except KeyError:
            raise Http404 from None

    def form_valid(self, form: "ModelForm[Channel]") -> HttpResponse:
        response = super().form_valid(form)
        audit.log_request(self.request, "notifications.channel_create", form.instance)
        return response


class ChannelUpdateView(AdminRequiredMixin, UpdateView):  # type: ignore[type-arg]
    model = Channel
    template_name = _CHANNEL_FORM_TEMPLATE
    success_url = reverse_lazy("notifications:channels")
    context_object_name = "channel"

    def get_form_class(self) -> type[ChannelForm]:
        return CHANNEL_FORMS[self.object.kind]

    def form_valid(self, form: "ModelForm[Channel]") -> HttpResponse:
        response = super().form_valid(form)
        # Field names only: the values may be secrets.
        audit.log_request(
            self.request, "notifications.channel_update", form.instance, changed=form.changed_data
        )
        return response


class ChannelDeleteView(AdminRequiredMixin, View):
    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        channel = get_object_or_404(Channel, pk=pk)
        audit.log_request(request, "notifications.channel_delete", channel)
        channel.delete()
        messages.success(request, _("The channel is deleted."))
        return redirect("notifications:channels")


class ChannelTestView(AdminRequiredMixin, View):
    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        send_test(get_object_or_404(Channel, pk=pk))
        messages.success(request, _("A test message is on its way. The result is in the log."))
        return redirect("notifications:log")


class DeliveryLogView(PageSizeMixin, AdminRequiredMixin, ListView):  # type: ignore[type-arg]
    template_name = "notifications/log.html"
    context_object_name = "deliveries"

    def get_queryset(self) -> QuerySet[Delivery]:
        self.filter_form = DeliveryFilterForm(self.request.GET or None)
        deliveries = Delivery.objects.select_related("notification", "notification__user")
        if self.filter_form.is_valid() and self.filter_form.cleaned_data["status"]:
            deliveries = deliveries.filter(status=self.filter_form.cleaned_data["status"])
        return deliveries

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        return super().get_context_data(filter_form=self.filter_form, **kwargs)
