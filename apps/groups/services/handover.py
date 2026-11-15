"""Handing the lead of a server group to another member: the checks, the operation and the keys.

The new leading server must be a clone of the group: the operation compares its files with
those of another member that answers, the leading server first. The keys of the group then
belong to it, with their traffic history; the former leader stays a member and may leave.
A leading server that does not answer is left behind as lagging and catches up later.
"""

from contextlib import ExitStack

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _

from amnezia_core.awg import AwgServerConfig
from amnezia_core.awg_backup import AwgServerFiles
from amnezia_core.awg_group import AwgGroupIdentity, Mismatch
from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.backups.services.snapshot import read_awg_files
from apps.groups.errors import GroupError, GroupErrorCode
from apps.groups.models import GroupMember, MemberState, ServerGroup
from apps.groups.services.addressing import GroupKeyAddress, client_host
from apps.groups.services.cloning import locked_source
from apps.groups.services.membership import SETTINGS_KIND
from apps.keys.models import AccessKey, Drift
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.servers.services.connection import connect
from apps.traffic.services.moving import move_key_traffic

HANDOVER_KIND = "groups.handover"

_UNFINISHED = (OperationStatus.PENDING, OperationStatus.RUNNING)


def start_handover(member: GroupMember, *, user: User, ip: str | None = None) -> Operation:
    """Start making the member the leading server of its group."""
    with transaction.atomic():
        # The group row serializes handovers of one group.
        group = ServerGroup.objects.select_for_update().get(pk=member.group_id)
        member = GroupMember.objects.select_related("instance").get(pk=member.pk)
        _check_candidate(member)
        running = Operation.objects.filter(status__in=_UNFINISHED, params__group_id=group.pk)
        if running.filter(kind=HANDOVER_KIND).exists():
            raise GroupError(GroupErrorCode.HANDOVER_RUNNING, str(group.pk))
        if running.filter(kind=SETTINGS_KIND).exists():
            raise GroupError(GroupErrorCode.SETTINGS_RUNNING, str(group.pk))
        leader = group.leader
        if leader is not None and keys_changing(leader.instance.source):
            raise GroupError(GroupErrorCode.KEYS_CHANGING, leader.instance.source)
        params = {
            "member_id": member.pk,
            "group_id": group.pk,
            "server_id": member.instance.server_id,
        }
        operation = launch(HANDOVER_KIND, params, user=user, subject=str(member))
        data = {"operation": operation.pk, "instance": member.instance_id}
        audit.log(HANDOVER_KIND, actor=user, target=group, data=data, ip=ip)
    return operation


def keys_changing(source: str) -> bool:
    """A key operation of the instance is queued or running: it may still use the old source."""
    keys = list(AccessKey.objects.filter(source=source).values_list("pk", flat=True))
    return Operation.objects.filter(
        Q(params__key_id__in=keys) | Q(params__source=source),
        kind__startswith="keys.",
        status__in=_UNFINISHED,
    ).exists()


def _check_candidate(member: GroupMember) -> None:
    if member.leading or member.state != MemberState.MEMBER or member.lagging:
        raise GroupError(GroupErrorCode.NOT_UP_TO_DATE, str(member.pk))


class LeaderHandover:
    """Makes a clone of the group its leading server; run again, it finds the work done."""

    def __init__(self, member: GroupMember, ctx: OperationContext) -> None:
        self._member = member
        self._instance = member.instance
        self._ctx = ctx

    def run(self) -> None:
        with ExitStack() as stack:
            with self._ctx.step(_("Read the files of the group")):
                identity, leader_answered = self._group_files(stack)
            with self._ctx.step(_("Connect to the server")):
                host = stack.enter_context(connect(self._instance.server))
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(host.lock())
            with self._ctx.step(_("Compare the server with the group")):
                self._check(read_awg_files(host), identity)
            # Both servers stay locked: no key change lands on them while the keys move.
            with self._ctx.step(_("Make the server the leading one")):
                self._take_lead(leader_answered=leader_answered)

    def _group_files(self, stack: ExitStack) -> tuple[AwgGroupIdentity | None, bool]:
        """The files of another member, locked in `stack`, and whether the leader gave them."""
        try:
            source, identity = stack.enter_context(locked_source(self._member, self._ctx))
        except GroupError as error:
            if error.code != GroupErrorCode.NO_IDENTITY:
                raise
            self._ctx.log(_("No other server of the group answers: only its keys are compared."))
            return None, False
        return identity, source.leading

    def _check(self, files: AwgServerFiles, identity: AwgGroupIdentity | None) -> None:
        """Raise GroupError unless the server runs the files of the group."""
        if identity is not None:
            kinds = sorted({divergence.kind for divergence in identity.compare(files)})
        else:
            kinds = self._key_divergences(files)
        if kinds:
            self._ctx.log(_("Differences with the group: %(kinds)s.") % {"kinds": ", ".join(kinds)})
            raise GroupError(GroupErrorCode.DIVERGED, ", ".join(kinds))
        self._ctx.log(_("The server is a clone of the group."))

    def _key_divergences(self, files: AwgServerFiles) -> list[Mismatch]:
        """Without another member to compare with, the server and preshared keys of the group."""
        leader = self._member.group.leader
        if leader is None:
            return []
        config = AwgServerConfig.parse(files.config)
        expected = leader.instance
        return [
            kind
            for kind, same in (
                (Mismatch.PRIVATE_KEY, config.private_key == expected.private_key),
                (
                    Mismatch.PRESHARED_KEY,
                    files.preshared_key.strip() == expected.preshared_key.strip(),
                ),
            )
            if not same
        ]

    @transaction.atomic
    def _take_lead(self, *, leader_answered: bool) -> None:
        members = {
            member.pk: member
            for member in GroupMember.objects.select_for_update()
            .filter(group_id=self._member.group_id)
            .select_related("instance__server")
        }
        target = members[self._member.pk]
        leader = next((member for member in members.values() if member.leading), None)
        _check_candidate(target)
        if leader is not None:
            self._step_down(leader, answered=leader_answered)
        target.leading = True
        target.save(update_fields=["leading"])
        if leader is not None:
            self._move_keys(leader.instance.source, target.instance.source)
        group = target.group
        host = client_host(group) or target.instance.server.host
        moved = GroupKeyAddress(group, host).apply()
        if moved:
            self._ctx.log(_("Keys moved to the new address: %(count)d.") % {"count": moved})
        self._ctx.log(_("%(server)s leads the group.") % {"server": target.instance.server})

    def _step_down(self, leader: GroupMember, *, answered: bool) -> None:
        if keys_changing(leader.instance.source):
            raise GroupError(GroupErrorCode.KEYS_CHANGING, leader.instance.source)
        leader.leading = False
        fields = ["leading"]
        if not answered and leader.lagging_since is None:
            leader.lagging_since = timezone.now()
            fields.append("lagging_since")
            self._ctx.log(
                _("%(server)s did not answer: it catches up with the group later.")
                % {"server": leader.instance.server}
            )
        leader.save(update_fields=fields)

    def _move_keys(self, old: str, new: str) -> None:
        """Keys of the group, with their traffic, become keys of the new leading server.

        Differences found on the former leader describe a server that no longer issues keys.
        """
        count = AccessKey.objects.filter(source=old).update(source=new)
        Drift.objects.filter(source=old).delete()
        move_key_traffic(old, new)
        self._ctx.log(_("Keys of the group moved: %(count)d.") % {"count": count})
