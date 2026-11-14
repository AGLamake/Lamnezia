"""Key changes reaching every member of a group, lagging members and their catch-up.

docs/FALLBACK.md section 6: a change goes to the members that answer; one that does not is
marked lagging, left out of the relay backends and caught up by a periodic operation.
"""

import logging
from collections.abc import Sequence
from functools import partial

from django.db.models import QuerySet
from django.utils import timezone

from apps.groups.models import GroupMember, MemberState, ServerGroup
from apps.keys.services.replicas import GroupServer, key_groups
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.launcher import launch
from apps.protocols.models import ProtocolInstance
from apps.servers.services.connection import connect
from apps.ssh.errors import SshError

logger = logging.getLogger(__name__)

CATCH_UP_KIND = "groups.catch_up"


def backends(group: ServerGroup) -> list[GroupMember]:
    """Members the relay sends clients to, in its order; a lagging one waits for its catch-up."""
    return list(_up_to_date(group.pk))


def _up_to_date(group_id: int) -> QuerySet[GroupMember]:
    return (
        GroupMember.objects.filter(group_id=group_id, state=MemberState.MEMBER, lagging_since=None)
        .select_related("instance__server")
        .order_by("-leading", "priority", "pk")
    )


class GroupReplicas:
    """The `KeyGroup` of keys issued on the leading server of a group."""

    def __init__(self, group_id: int) -> None:
        self._group_id = group_id
        self._servers: dict[GroupServer, int] = {}

    def members(self) -> Sequence[GroupServer]:
        """Members up to date now; those left out are checked again in `finish`."""
        self._servers = {
            GroupServer(str(member.instance.server), partial(connect, member.instance.server)): (
                member.pk
            )
            for member in _up_to_date(self._group_id)
        }
        return list(self._servers)

    def fall_behind(self, server: GroupServer) -> None:
        GroupMember.objects.filter(pk=self._servers[server], lagging_since=None).update(
            lagging_since=timezone.now()
        )

    def finish(self) -> None:
        """A member that joined or caught up during the change may lack it: it lags."""
        missed = _up_to_date(self._group_id).exclude(pk__in=self._servers.values())
        missed.update(lagging_since=timezone.now())


@key_groups.register
def group_of(source: str) -> GroupReplicas | None:
    """The group whose leading server issues the keys of `source`; None for a standalone one."""
    instance_id = ProtocolInstance.pk_of(source)
    if instance_id is None:
        return None
    group_id = (
        GroupMember.objects.filter(instance_id=instance_id, leading=True)
        .values_list("group_id", flat=True)
        .first()
    )
    return None if group_id is None else GroupReplicas(group_id)


def catch_up_lagging() -> None:
    """Start a catch-up for every lagging member that answers and has none running yet."""
    lagging = GroupMember.objects.filter(
        state=MemberState.MEMBER, lagging_since__isnull=False
    ).select_related("instance__server")
    for member in lagging:
        if _catching_up(member):
            continue
        server = member.instance.server
        try:
            with connect(server):
                pass
        except SshError as error:
            logger.info("lagging member %s is still unreachable: %s", member.pk, error.code)
            continue
        params = {"member_id": member.pk, "server_id": server.pk}
        launch(CATCH_UP_KIND, params, subject=str(member))


def _catching_up(member: GroupMember) -> bool:
    return Operation.objects.filter(
        kind=CATCH_UP_KIND,
        status__in=(OperationStatus.PENDING, OperationStatus.RUNNING),
        params__member_id=member.pk,
    ).exists()
