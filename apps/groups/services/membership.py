"""Creating server groups and starting servers joining and leaving them (FALLBACK.md section 4).

The panel side only: who may join, the member rows and the operations. Joining and leaving
change files on the server, so they run as background operations (`apps.groups.operations`).
"""

from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from amnezia_core.awg import AwgServerConfig
from amnezia_core.awg_backup import AwgServerFiles
from amnezia_core.awg_export import AWG3_VERSION
from amnezia_core.containers import AWG2
from amnezia_core.keys import KeyPair
from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.groups.errors import GroupError, GroupErrorCode
from apps.groups.models import GroupMember, MemberState, ServerGroup
from apps.keys.models import GONE_STATUSES, AccessKey, KeyStatus
from apps.operations.models import Operation
from apps.operations.services.launcher import launch
from apps.protocols.models import ProtocolInstance

CREATE_KIND = "groups.create"
DELETE_KIND = "groups.delete"
JOIN_KIND = "groups.join"
LEAVE_KIND = "groups.leave"


def check_standalone(instance: ProtocolInstance) -> None:
    """Raise GroupError unless the instance is a standalone AmneziaWG 3.1 server."""
    if instance.container != AWG2.name or instance.awg_version != AWG3_VERSION:
        raise GroupError(GroupErrorCode.NOT_AWG3, instance.source)
    if instance.removed:
        raise GroupError(GroupErrorCode.CONTAINER_REMOVED, instance.source)
    if GroupMember.objects.filter(instance=instance).exists():
        raise GroupError(GroupErrorCode.IN_GROUP, instance.source)


def standalone_instances() -> list[ProtocolInstance]:
    """AmneziaWG 3.1 servers that may lead a new group or join one."""
    candidates = ProtocolInstance.objects.filter(
        container=AWG2.name, container_removed_at__isnull=True, group_member__isnull=True
    ).select_related("server")
    return [instance for instance in candidates if instance.awg_version == AWG3_VERSION]


def live_keys(instance: ProtocolInstance) -> int:
    """Keys of the instance that stop working when its server takes another identity."""
    return (
        AccessKey.objects.filter(source=instance.source).exclude(status__in=GONE_STATUSES).count()
    )


@transaction.atomic
def create_group(
    group: ServerGroup, leader: ProtocolInstance, *, actor: User, ip: str | None = None
) -> ServerGroup:
    """Save the new `group` with the leader as it is as its identity: nothing changes on the
    server; the group takes the leader's port.
    """
    check_standalone(leader)
    group.port = leader.port
    group.save()
    _add_member(group, leader, leading=True, state=MemberState.MEMBER, joined_at=timezone.now())
    audit.log(CREATE_KIND, actor=actor, target=group, data={"leader": leader.pk}, ip=ip)
    return group


@transaction.atomic
def delete_group(group: ServerGroup, *, actor: User, ip: str | None = None) -> None:
    """Forget a group with only its leader left; the leader stays a standalone server as is."""
    if group.members.filter(leading=False).exists():
        raise GroupError(GroupErrorCode.MEMBERS_LEFT, str(group.pk))
    audit.log(DELETE_KIND, actor=actor, target=group, data={"name": group.name}, ip=ip)
    group.delete()


def start_join(
    group: ServerGroup, instance: ProtocolInstance, *, user: User, ip: str | None = None
) -> Operation:
    """Reserve the instance for the group and start copying the group files onto it."""
    with transaction.atomic():
        check_standalone(instance)
        leader = group.leader
        if instance.port != group.port:
            raise GroupError(GroupErrorCode.OTHER_PORT, instance.source)
        if leader is not None and instance.subnet != leader.instance.subnet:
            raise GroupError(GroupErrorCode.OTHER_SUBNET, instance.source)
        member = _add_member(group, instance)
        params = {"member_id": member.pk, "server_id": instance.server_id}
        operation = launch(JOIN_KIND, params, user=user, subject=str(member))
        data = {"operation": operation.pk, "instance": instance.pk}
        audit.log(JOIN_KIND, actor=user, target=group, data=data, ip=ip)
    return operation


def start_leave(member: GroupMember, *, user: User, ip: str | None = None) -> Operation:
    """Start giving the member's server a clean AmneziaWG; the group keeps its keys."""
    with transaction.atomic():
        member = GroupMember.objects.select_for_update().select_related("group").get(pk=member.pk)
        if member.leading:
            raise GroupError(GroupErrorCode.LEADER_LEAVES, str(member.pk))
        member.state = MemberState.LEAVING
        member.save(update_fields=["state"])
        params = {"member_id": member.pk, "server_id": member.instance.server_id}
        operation = launch(LEAVE_KIND, params, user=user, subject=str(member))
        data = {"operation": operation.pk, "instance": member.instance_id}
        audit.log(LEAVE_KIND, actor=user, target=member.group, data=data, ip=ip)
    return operation


@transaction.atomic
def take_identity(instance: ProtocolInstance, files: AwgServerFiles) -> list[AccessKey]:
    """Save the server key and settings the server now runs with; the keys it revoked.

    Keys issued for the old server key do not connect any more: they are revoked without a
    notice, the group gives their owners its own keys.
    """
    config = AwgServerConfig.parse(files.config)
    private_key = config.private_key or ""
    revoked: list[AccessKey] = []
    if private_key != instance.private_key:
        keys = AccessKey.objects.filter(source=instance.source).exclude(status__in=GONE_STATUSES)
        revoked = list(keys.select_related("user").select_for_update(of=("self",)))
        keys.filter(pk__in=[key.pk for key in revoked]).update(
            status=KeyStatus.REVOKED, revoked_at=timezone.now()
        )
    instance.private_key = private_key
    instance.public_key = KeyPair.from_private_key(private_key).public_key
    instance.preshared_key = files.preshared_key
    instance.params = config.awg_params
    instance.port = config.listen_port or instance.port
    instance.subnet = str(config.address.network)
    instance.save(
        update_fields=["private_key", "public_key", "preshared_key", "params", "port", "subnet"]
    )
    return revoked


def _add_member(group: ServerGroup, instance: ProtocolInstance, **fields: object) -> GroupMember:
    last = group.members.aggregate(last=Max("priority"))["last"]
    try:
        with transaction.atomic():
            return GroupMember.objects.create(
                group=group,
                instance=instance,
                priority=0 if last is None else last + 1,
                **fields,
            )
    except IntegrityError as error:  # another request put the instance into a group meanwhile
        raise GroupError(GroupErrorCode.IN_GROUP, instance.source) from error
