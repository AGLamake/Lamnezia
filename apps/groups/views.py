"""Pages of the server groups: the list, a new group, its members, editing and settings, joining,
leaving, the lead, a planned switch of the relay."""

from typing import Any

from django.contrib import messages
from django.db import transaction
from django.db.models import QuerySet
from django.forms import ModelForm
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.functional import cached_property
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views import View
from django.views.generic import CreateView, DetailView, FormView, ListView, UpdateView

from apps.accounts.permissions import AdminRequiredMixin
from apps.groups.errors import GroupError, GroupErrorCode
from apps.groups.forms import GroupEditForm, GroupForm, GroupSettingsForm
from apps.groups.models import GroupMember, ServerGroup
from apps.groups.services.addressing import DomainCheck, client_host
from apps.groups.services.handover import key_operations, start_handover
from apps.groups.services.membership import (
    cancel_join,
    create_group,
    delete_group,
    join_failed,
    joining_operations,
    leave_failed,
    live_keys,
    port_taken,
    retry_join,
    retry_leave,
    servers_without_awg,
    standalone_instances,
    start_install,
    start_join,
    start_leave,
    stay_in_group,
    update_group,
)
from apps.groups.services.settings import changing_settings
from apps.groups.tasks import check_group_domain
from apps.keys.services.counts import InstanceKeyCount
from apps.protocols.models import ProtocolInstance
from apps.relays.errors import RelayError
from apps.relays.models import Relay
from apps.relays.services.health import GroupHealth
from apps.relays.services.switch import start_switch
from apps.servers.models import Server


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


class GroupEditView(AdminRequiredMixin, UpdateView):  # type: ignore[type-arg]
    """The name, domain and relay of a group; its keys follow a new address."""

    model = ServerGroup
    form_class = GroupEditForm
    template_name = "groups/edit.html"
    context_object_name = "group"

    def form_valid(self, form: "ModelForm[ServerGroup]") -> HttpResponse:
        try:
            moved = update_group(form.save(commit=False), actor=self.member, ip=self.client_ip)
        except GroupError as error:
            form.add_error(None, f"{error.message} {error.hint}")
            return self.form_invalid(form)
        if moved:
            text = ngettext(
                "The group is saved; %(count)d key moved to the new address.",
                "The group is saved; %(count)d keys moved to the new address.",
                moved,
            )
            messages.success(self.request, text % {"count": moved})
        else:
            messages.success(self.request, _("The group is saved."))
        return redirect("groups:detail", pk=self.object.pk)


class GroupDetailView(AdminRequiredMixin, DetailView):  # type: ignore[type-arg]
    """The members of a group, the standalone servers that may join it and those without AWG.

    The operations under way that a change of the group waits for are listed with links: joins
    and installs, and the key operations that hold the lead. `?leader_leaves=1` (after a leading
    server was asked to leave) offers to hand the lead to another member first.
    """

    model = ServerGroup
    template_name = "groups/detail.html"
    context_object_name = "group"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        group: ServerGroup = self.object
        # A server cannot be both the relay of the group and a member: they share the port.
        candidates = [
            (instance, live_keys(instance))
            for instance in standalone_instances()
            if instance.port == group.port and instance.server_id != group.relay_id
        ]
        relay = Relay.objects.filter(server_id=group.relay_id).first() if group.relay_id else None
        health = relay.group_health(group.port) if relay else None
        members = list(group.members.select_related("instance__server"))
        leader = next((member for member in members if member.leading), None)
        return super().get_context_data(
            members=[
                (member, *_seen_by_relay(member, health), leave_failed(member), join_failed(member))
                for member in members
            ],
            successors=[member for member in members if _may_lead(member)],
            leader_leaves=bool(self.request.GET.get("leader_leaves")) and len(members) > 1,
            joining=list(joining_operations(group)),
            key_operations=list(key_operations(leader.instance.source)) if leader else [],
            relay=relay,
            health=health,
            client_host=client_host(group),
            domain_warnings=_domain_warnings(group),
            candidates=candidates,
            bare_servers=[
                (server, port_taken(server, group.port)) for server in servers_without_awg()
            ],
            **kwargs,
        )


def _domain_warnings(group: ServerGroup) -> list[str]:
    """Warnings about the domain from the last check; a missing check is queued, not awaited."""
    check = DomainCheck(group)
    if check.needed and check.answers() is None:
        transaction.on_commit(lambda: check_group_domain.delay(group.pk))
    return check.warnings()


def _may_lead(member: GroupMember) -> bool:
    """Whether the page offers to make the member leading; the operation checks it again."""
    return not member.leading and member.joined and not member.lagging


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


class GroupInstallView(AdminRequiredMixin, View):
    def post(self, request: HttpRequest, pk: int, server_pk: int) -> HttpResponse:
        group = get_object_or_404(ServerGroup, pk=pk)
        server = get_object_or_404(Server, pk=server_pk)
        try:
            start_install(group, server, user=self.member, ip=self.client_ip)
        except GroupError as error:
            messages.error(request, f"{error.message} {error.hint}")
        return redirect("groups:detail", pk=group.pk)


class GroupJoinRetryView(AdminRequiredMixin, View):
    def post(self, request: HttpRequest, member_pk: int) -> HttpResponse:
        member = get_object_or_404(GroupMember, pk=member_pk)
        try:
            retry_join(member, user=self.member, ip=self.client_ip)
        except GroupError as error:
            messages.error(request, f"{error.message} {error.hint}")
        return redirect("groups:detail", pk=member.group_id)


class GroupJoinCancelView(AdminRequiredMixin, View):
    def post(self, request: HttpRequest, member_pk: int) -> HttpResponse:
        member = get_object_or_404(GroupMember, pk=member_pk)
        try:
            cancel_join(member, user=self.member, ip=self.client_ip)
        except GroupError as error:
            messages.error(request, f"{error.message} {error.hint}")
        return redirect("groups:detail", pk=member.group_id)


class GroupLeaveView(AdminRequiredMixin, View):
    def post(self, request: HttpRequest, member_pk: int) -> HttpResponse:
        member = get_object_or_404(GroupMember, pk=member_pk)
        try:
            start_leave(member, user=self.member, ip=self.client_ip)
        except GroupError as error:
            if error.code == GroupErrorCode.LEADER_LEAVES:
                # The group page shows the error with the members that may take the lead.
                url = reverse("groups:detail", kwargs={"pk": member.group_id})
                return redirect(f"{url}?leader_leaves=1#leader-leaves")
            messages.error(request, f"{error.message} {error.hint}")
        return redirect("groups:detail", pk=member.group_id)


class GroupLeaveRetryView(AdminRequiredMixin, View):
    def post(self, request: HttpRequest, member_pk: int) -> HttpResponse:
        member = get_object_or_404(GroupMember, pk=member_pk)
        try:
            retry_leave(member, user=self.member, ip=self.client_ip)
        except GroupError as error:
            messages.error(request, f"{error.message} {error.hint}")
        return redirect("groups:detail", pk=member.group_id)


class GroupStayView(AdminRequiredMixin, View):
    def post(self, request: HttpRequest, member_pk: int) -> HttpResponse:
        member = get_object_or_404(GroupMember, pk=member_pk)
        try:
            stay_in_group(member, user=self.member, ip=self.client_ip)
        except GroupError as error:
            messages.error(request, f"{error.message} {error.hint}")
        else:
            messages.success(
                request,
                _("The server stays in the group; the panel is copying the group files onto it."),
            )
        return redirect("groups:detail", pk=member.group_id)


class GroupHandoverView(AdminRequiredMixin, View):
    def post(self, request: HttpRequest, member_pk: int) -> HttpResponse:
        member = get_object_or_404(GroupMember, pk=member_pk)
        try:
            start_handover(member, user=self.member, ip=self.client_ip)
        except GroupError as error:
            messages.error(request, f"{error.message} {error.hint}")
        return redirect("groups:detail", pk=member.group_id)


class GroupSwitchView(AdminRequiredMixin, View):
    """The relay sends the clients of the group to the member, for maintenance of the active one."""

    def post(self, request: HttpRequest, member_pk: int) -> HttpResponse:
        member = get_object_or_404(GroupMember, pk=member_pk)
        try:
            start_switch(member, user=self.member, ip=self.client_ip)
        except RelayError as error:
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


class GroupSettingsView(AdminRequiredMixin, FormView):  # type: ignore[type-arg]
    """The protocol settings of every member of a group; saving starts the operation."""

    template_name = "groups/settings.html"
    form_class = GroupSettingsForm

    @cached_property
    def group(self) -> ServerGroup:
        return get_object_or_404(ServerGroup, pk=self.kwargs["pk"])

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return self._without_leader(request) or super().get(request, *args, **kwargs)

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        return self._without_leader(request) or super().post(request, *args, **kwargs)

    def _without_leader(self, request: HttpRequest) -> HttpResponse | None:
        """Back to the group page if it has no leading server to take the settings from."""
        if self.group.leader is not None:
            return None
        messages.error(request, _("The group has no leading server."))
        return redirect("groups:detail", pk=self.group.pk)

    def get_form_kwargs(self) -> dict[str, Any]:
        return {**super().get_form_kwargs(), "group": self.group}

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        form: GroupSettingsForm = kwargs.get("form") or self.get_form()
        keys = InstanceKeyCount.of(form.instance.source)
        return super().get_context_data(
            group=self.group,
            instance=form.instance,
            keys=keys.total,
            imported=keys.imported,
            busy=changing_settings(self.group),
            **kwargs,
        )

    def form_valid(self, form: GroupSettingsForm) -> HttpResponse:
        try:
            operation = form.start(self.member, self.client_ip)
        except GroupError as error:
            form.add_error(None, f"{error.message} {error.hint}")
            return self.form_invalid(form)
        return redirect("operations:detail", pk=operation.pk)
