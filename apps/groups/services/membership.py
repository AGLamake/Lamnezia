"""Creating server groups and starting servers joining and leaving them (FALLBACK.md section 4).

The panel side only: who may join, the member rows and the operations. Joining and leaving
change files on the server, so they run as background operations (`apps.groups.operations`).
A server without AmneziaWG joins by an install straight with the files of the group.
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
from apps.groups.services.addressing import GroupKeyAddress
from apps.keys.models import GONE_STATUSES, AccessKey, KeyStatus
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.launcher import launch
from apps.protocols.models import InstanceOrigin, ProtocolInstance
from apps.protocols.services.installation import used_ports
from apps.servers.models import Server

CREATE_KIND = "groups.create"
DELETE_KIND = "groups.delete"
JOIN_KIND = "groups.join"
INSTALL_KIND = "groups.install"
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


def servers_without_awg() -> list[Server]:
    """Servers that may get AmneziaWG 3.1 straight with the files of a group.

    They have no amnezia-awg2 container, known to the panel or seen on the server, and no relay.
    """
    servers = Server.objects.exclude(instances__container=AWG2.name).filter(relay_role__isnull=True)
    return [server for server in servers if not _has_awg_container(server)]


def port_taken(server: Server, port: int) -> bool:
    """Whether the panel knows the UDP `port` to be taken on the server."""
    return (port, "udp") in used_ports(server)


def check_without_awg(group: ServerGroup, server: Server) -> None:
    """Raise GroupError unless the server may get AmneziaWG with the files of the group."""
    if _has_awg_container(server) or server.instances.filter(container=AWG2.name).exists():
        raise GroupError(GroupErrorCode.AWG_ON_SERVER, server.name)
    if hasattr(server, "relay_role"):
        raise GroupError(GroupErrorCode.RELAY_SERVER, server.name)
    if port_taken(server, group.port):
        raise GroupError(GroupErrorCode.PORT_TAKEN, f"{server.name}: udp {group.port}")


def _has_awg_container(server: Server) -> bool:
    return any(container.get("name") == AWG2.name for container in server.containers)


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
    server; the group takes the leader's port. With its relay running, the leader's keys move to
    the relay address.
    """
    check_standalone(leader)
    group.port = leader.port
    group.save()
    _add_member(group, leader, leading=True, state=MemberState.MEMBER, joined_at=timezone.now())
    GroupKeyAddress(group).apply()
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


def start_install(
    group: ServerGroup, server: Server, *, user: User, ip: str | None = None
) -> Operation:
    """Start installing AmneziaWG on a server without it, straight with the files of the group.

    The server has no clients, so nothing is kept first; it becomes a member when the install
    succeeds.
    """
    with transaction.atomic():
        server = Server.objects.select_for_update().get(pk=server.pk)
        check_without_awg(group, server)
        if _installing(server):
            raise GroupError(GroupErrorCode.JOIN_RUNNING, server.name)
        params = {"group_id": group.pk, "server_id": server.pk}
        operation = launch(INSTALL_KIND, params, user=user, subject=f"{group} · {server}")
        data = {"operation": operation.pk, "server": server.pk}
        audit.log(INSTALL_KIND, actor=user, target=group, data=data, ip=ip)
    return operation


def _installing(server: Server) -> bool:
    return Operation.objects.filter(
        kind=INSTALL_KIND,
        status__in=(OperationStatus.PENDING, OperationStatus.RUNNING),
        params__server_id=server.pk,
    ).exists()


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


@transaction.atomic
def add_installed(group: ServerGroup, server: Server, files: AwgServerFiles) -> GroupMember:
    """Save the AmneziaWG an install put on `server` with the files of the group as a member.

    Run again after a failure, it finds the member already saved; an instance of another
    AmneziaWG on the server is refused.
    """
    existing = ProtocolInstance.objects.filter(server=server, container=AWG2.name).first()
    if existing is not None:
        member = GroupMember.objects.filter(instance=existing, group=group).first()
        if member is None:
            raise GroupError(GroupErrorCode.AWG_ON_SERVER, server.name)
        return member
    config = AwgServerConfig.parse(files.config)
    private_key = config.private_key or ""
    port = config.listen_port or group.port
    instance = ProtocolInstance.objects.create(
        server=server,
        container=AWG2.name,
        port=port,
        subnet=str(config.address.network),
        params=config.awg_params,
        private_key=private_key,
        public_key=KeyPair.from_private_key(private_key).public_key,
        preshared_key=files.preshared_key,
        origin=InstanceOrigin.INSTALLED,
    )
    others = [c for c in server.containers if c.get("name") != AWG2.name]
    server.record_containers([*others, {"name": AWG2.name, "port": port, "transport": "udp"}])
    return _add_member(group, instance, state=MemberState.MEMBER, joined_at=timezone.now())


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
