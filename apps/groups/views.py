"""Pages of the server groups: the list, a new group, its members, joining and leaving."""

from typing import Any

from django.contrib import messages
from django.db.models import QuerySet
from django.forms import ModelForm
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import CreateView, DetailView, ListView

from apps.accounts.permissions import AdminRequiredMixin
from apps.groups.errors import GroupError
from apps.groups.forms import GroupForm
from apps.groups.models import GroupMember, ServerGroup
from apps.groups.services.addressing import DomainCheck, client_host
from apps.groups.services.membership import (
    create_group,
    delete_group,
    live_keys,
    standalone_instances,
    start_join,
    start_leave,
)
from apps.protocols.models import ProtocolInstance
from apps.relays.models import Relay
from apps.relays.services.health import GroupHealth


class GroupListView(AdminRequiredMixin, ListView):  # type: ignore[type-arg]
    template_name = "groups/list.html"
    context_object_name = "groups"

    def get_queryset(self) -> QuerySet[ServerGroup]:
        return ServerGroup.objects.prefetch_related("members__instance__server").order_by("name")


class GroupCreateView(AdminRequiredMixin, CreateView):  # type: ignore[type-arg]
    form_class = GroupForm
    template_name = "groups/form.html"

    def form_valid(self, form: "ModelForm[ServerGroup]") -> HttpResponse:
        try:
            group = create_group(
                form.save(commit=False),
                form.cleaned_data["leader"],
                actor=self.member,
                ip=self.client_ip,
            )
        except GroupError as error:
            form.add_error(None, error.message)
            return self.form_invalid(form)
        return redirect("groups:detail", pk=group.pk)


class GroupDetailView(AdminRequiredMixin, DetailView):  # type: ignore[type-arg]
    """The members of a group and the standalone servers that may join it."""

    model = ServerGroup
    template_name = "groups/detail.html"
    context_object_name = "group"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        group: ServerGroup = self.object
        candidates = [
            (instance, live_keys(instance))
            for instance in standalone_instances()
            if instance.port == group.port
        ]
        relay = Relay.objects.filter(server_id=group.relay_id).first() if group.relay_id else None
        health = relay.group_health(group.port) if relay else None
        members = group.members.select_related("instance__server")
        return super().get_context_data(
            members=[(member, *_seen_by_relay(member, health)) for member in members],
            relay=relay,
            health=health,
            client_host=client_host(group),
            domain_warnings=DomainCheck(group).warnings(),
            candidates=candidates,
            **kwargs,
        )


def _seen_by_relay(member: GroupMember, health: GroupHealth | None) -> tuple[bool | None, bool]:
    """Whether the relay reaches the member (None: unknown) and whether it is the active one."""
    if health is None:
        return None, False
    active = health.active is not None and health.active.id == member.pk
    return health.reachable(member.pk), active


class GroupJoinView(AdminRequiredMixin, View):
    def post(self, request: HttpRequest, pk: int, instance_pk: int) -> HttpResponse:
        group = get_object_or_404(ServerGroup, pk=pk)
        instance = get_object_or_404(ProtocolInstance, pk=instance_pk)
        try:
            start_join(group, instance, user=self.member, ip=self.client_ip)
        except GroupError as error:
            messages.error(request, f"{error.message} {error.hint}")
        return redirect("groups:detail", pk=group.pk)


class GroupLeaveView(AdminRequiredMixin, View):
    def post(self, request: HttpRequest, member_pk: int) -> HttpResponse:
        member = get_object_or_404(GroupMember, pk=member_pk)
        try:
            start_leave(member, user=self.member, ip=self.client_ip)
        except GroupError as error:
            messages.error(request, f"{error.message} {error.hint}")
        return redirect("groups:detail", pk=member.group_id)


class GroupDeleteView(AdminRequiredMixin, View):
    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        group = get_object_or_404(ServerGroup, pk=pk)
        try:
            delete_group(group, actor=self.member, ip=self.client_ip)
        except GroupError as error:
            messages.error(request, f"{error.message} {error.hint}")
            return redirect("groups:detail", pk=group.pk)
        messages.success(request, _("The group is deleted; its leading server stays as it is."))
        return redirect("groups:list")
