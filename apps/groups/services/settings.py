"""Protocol settings of a server group: changed once for the group and applied on every member.

Members are clones (docs/FALLBACK.md section 3), so the obfuscation and the subnet are settings
of the group, not of one server. They are changed on the group page only. The change goes to the
members that answer, all of them locked until it is over, so no key change lands between two of
them; one that does not answer or fails lags and the catch-up gives it the files of the group.
The port stays the port of the group, and each server keeps its own container log setting.
"""

from contextlib import ExitStack
from dataclasses import replace

from django.db import transaction
from django.utils.translation import gettext as _

from amnezia_core.containers import AWG2
from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.groups.errors import GroupError, GroupErrorCode
from apps.groups.models import GroupMember, LagReason, MemberState, ServerGroup
from apps.groups.services.handover import HANDOVER_KIND
from apps.groups.services.membership import SETTINGS_KIND
from apps.groups.services.replication import backends
from apps.keys.models import AccessKey
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.protocols.models import ProtocolInstance
from apps.protocols.services.awg_settings import AwgReconfiguration, AwgSettings, InstanceSettings
from apps.servers.services.connection import connect
from apps.ssh.errors import CatalogError, SshError
from apps.ssh.services import RemoteHost

_UNFINISHED = (OperationStatus.PENDING, OperationStatus.RUNNING)


def leading_instance(group: ServerGroup) -> ProtocolInstance:
    """The instance whose settings are those of the group: the keys of the group take them."""
    leader = group.leader
    if leader is None:
        raise GroupError(GroupErrorCode.NO_LEADER, str(group.pk))
    return leader.instance


def changing_settings(group: ServerGroup) -> bool:
    """A settings change or a handover of the group is queued or running."""
    return Operation.objects.filter(
        kind__in=(SETTINGS_KIND, HANDOVER_KIND),
        status__in=_UNFINISHED,
        params__group_id=group.pk,
    ).exists()


def start_group_settings(
    group: ServerGroup,
    settings: AwgSettings,
    *,
    user: User,
    reissue: bool,
    ip: str | None = None,
) -> Operation:
    """Start applying the settings on every member; `reissue`: as on a protocol settings page."""
    with transaction.atomic():
        # The group row serializes the changes of one group with its handovers.
        group = ServerGroup.objects.select_for_update().get(pk=group.pk)
        leading_instance(group)
        if changing_settings(group):
            raise GroupError(GroupErrorCode.SETTINGS_RUNNING, str(group.pk))
        params = {
            "group_id": group.pk,
            "user_id": user.pk,
            "reissue": reissue,
            **settings.to_params(),
        }
        operation = launch(SETTINGS_KIND, params, user=user, subject=str(group))
        data = Operation.without_secrets(params)
        audit.log(SETTINGS_KIND, actor=user, target=group, data=data, ip=ip)
    return operation


class GroupSettingsChange:
    """Applies new settings on the members that answer and saves them for the whole group.

    The first member that answers must take them, or nothing changes; any other that fails is
    rolled back by the reconfiguration and left to catch up. Run again, a member that already
    has the settings is not restarted.
    """

    def __init__(self, group: ServerGroup, ctx: OperationContext) -> None:
        self._group = group
        self._ctx = ctx
        self._leader = leading_instance(group)

    def run(self, settings: AwgSettings, *, actor: User | None, reissue: bool) -> None:
        """Apply the settings, then reissue the imported keys if asked and tell the owners.

        The port of the group and the container log of each server are not the group's to
        change: they are kept whatever `settings` says.
        """
        current = AwgSettings.of(self._leader)
        settings = replace(settings.within(current), port=current.port, logs=current.logs)
        if settings.touches_server(current):
            keys = self._change_servers(settings)
        else:
            with self._ctx.step(_("Update the keys")):
                self._ctx.log(_("Only the DNS changes: the servers are not touched."))
                keys = self._save(settings, changed=[])
        panel = InstanceSettings(self._leader, self._ctx)
        with self._ctx.step(_("Tell the owners of the keys")):
            if reissue:
                panel.reissue_imported(actor)
            if settings.changes_profiles(current):
                panel.tell_owners(keys)

    def _change_servers(self, settings: AwgSettings) -> list[AccessKey]:
        with ExitStack() as stack:
            with self._ctx.step(_("Connect to the servers of the group")):
                reached = self._lock_members(stack)
            changed = self._apply(reached, settings)
            with self._ctx.step(_("Save the result in the panel")):
                keys = self._save(settings, changed=[member.instance for member in changed])
                self._lag_others(changed)
        return keys

    def _lock_members(self, stack: ExitStack) -> list[tuple[GroupMember, RemoteHost]]:
        """Up-to-date members that answer, each locked in `stack`; the others lag."""
        reached: list[tuple[GroupMember, RemoteHost]] = []
        for member in backends(self._group):
            server = member.instance.server
            try:
                with ExitStack() as attempt:
                    host = attempt.enter_context(connect(server))
                    attempt.enter_context(host.lock())
                    stack.enter_context(attempt.pop_all())
            except SshError as error:
                self._ctx.log(
                    _("%(server)s: %(error)s") % {"server": server, "error": error.message}
                )
                continue
            reached.append((member, host))
        if not reached:
            raise GroupError(GroupErrorCode.NO_MEMBER_ANSWERS, str(self._group.pk))
        return reached

    def _apply(
        self, reached: list[tuple[GroupMember, RemoteHost]], settings: AwgSettings
    ) -> list[GroupMember]:
        """The members that took the settings; the first one must, the others may fail."""
        (first, first_host), *others = reached
        self._reconfigure(first, first_host, settings)
        changed = [first]
        for member, host in others:
            try:
                self._reconfigure(member, host, settings)
            except CatalogError as error:
                server = member.instance.server
                with self._ctx.step(_("Leave %(server)s to catch up") % {"server": server}):
                    self._ctx.log(error.message)
                continue
            changed.append(member)
        return changed

    def _reconfigure(self, member: GroupMember, host: RemoteHost, settings: AwgSettings) -> None:
        instance = member.instance
        own = replace(settings, logs=instance.container_logs)
        with self._ctx.step(_("Apply the settings on %(server)s") % {"server": instance.server}):
            AwgReconfiguration(host, self._ctx, AWG2).apply_locked(own)

    @transaction.atomic
    def _save(self, settings: AwgSettings, *, changed: list[ProtocolInstance]) -> list[AccessKey]:
        """The leading instance and the keys of the group take the settings; every member takes
        the DNS, and those that run with the new settings take them all. Returns the keys.
        """
        keys = InstanceSettings(self._leader, self._ctx).save(settings)
        changed_ids = {instance.pk for instance in changed}
        members = ProtocolInstance.objects.filter(group_member__group=self._group).exclude(
            pk=self._leader.pk
        )
        for instance in members:
            instance.primary_dns, instance.secondary_dns = settings.dns
            fields = ["primary_dns", "secondary_dns"]
            if instance.pk in changed_ids:
                instance.params = settings.params
                instance.subnet = settings.subnet
                fields += ["params", "subnet"]
            instance.save(update_fields=fields)
        return keys

    def _lag_others(self, changed: list[GroupMember]) -> None:
        """A member that did not take the settings, or joined or caught up meanwhile, lags."""
        missed = GroupMember.objects.filter(
            group=self._group, state=MemberState.MEMBER, lagging_since=None
        ).exclude(pk__in=[member.pk for member in changed])
        for member in missed.select_related("instance__server"):
            self._ctx.log(
                _("%(server)s catches up with the settings later.")
                % {"server": member.instance.server}
            )
        missed.fall_behind(LagReason.MISSED)
