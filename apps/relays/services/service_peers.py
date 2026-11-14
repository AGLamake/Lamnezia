"""The service peer of a group on its members: the client the relay agent probes them with.

It is made on the first member that answers, which gives it a free address of the group, and
put with that address on the others, as a key of the group is (apps/keys/services/replicas.py).
A member that cannot take it lags and gets it with the leader files on its catch-up.
"""

from contextlib import ExitStack
from ipaddress import IPv4Address

from django.utils.translation import gettext as _

from amnezia_core.awg import Peer
from amnezia_core.keys import KeyPair
from apps.groups.models import ServerGroup
from apps.groups.services.replication import GroupReplicas
from apps.keys.models import DISABLED_STATUSES, AccessKey
from apps.keys.services.peers import PeerManager
from apps.keys.services.replicas import GroupServer
from apps.keys.services.service_peers import service_peers
from apps.operations.services.context import OperationContext
from apps.protocols.models import ProtocolInstance
from apps.relays.errors import RelayError, RelayErrorCode
from apps.relays.models import ServicePeer
from apps.ssh.errors import CatalogError, SshError
from apps.ssh.services import RemoteHost

# The name the Amnezia app shows for the peer in the client list of a member.
PEER_NAME = "Lamnezia relay"


class ServicePeerPlacement:
    """Makes sure every member of a group that is up to date has the service peer."""

    def __init__(self, group: ServerGroup, ctx: OperationContext) -> None:
        self._group = group
        self._ctx = ctx
        self._replicas = GroupReplicas(group.pk)

    def place(self) -> None:
        """Raises RelayError when no member answers; nothing is changed then."""
        leader = self._group.leader
        if leader is None:
            return
        instance = leader.instance
        with ExitStack() as stack:
            reached = self._connect(stack)
            peer = ServicePeer.objects.filter(group=self._group).first()
            if peer is None:
                peer = self._create(reached[0], instance.source)
                reached = reached[1:]
            for server in reached:
                self._put(server, peer, instance.preshared_key)
        self._replicas.finish()

    def _connect(self, stack: ExitStack) -> list[tuple[GroupServer, RemoteHost]]:
        reached: list[tuple[GroupServer, RemoteHost]] = []
        with self._ctx.step(_("Connect to the servers of %(group)s") % {"group": self._group.name}):
            for server in self._replicas.members():
                try:
                    reached.append((server, stack.enter_context(server.connect())))
                except SshError as error:
                    self._ctx.log(f"{server.name}: {error.message}")
                    self._replicas.fall_behind(server)
            if not reached:
                raise RelayError(RelayErrorCode.NO_MEMBER, self._group.name)
        return reached

    def _create(self, reached: tuple[GroupServer, RemoteHost], source: str) -> ServicePeer:
        server, host = reached
        pair = KeyPair.generate()
        with self._ctx.step(_("Add the service peer on %(server)s") % {"server": server.name}):
            added = PeerManager(host, self._ctx).add(
                pair.public_key, PEER_NAME, reserved=_reserved(source)
            )
        return ServicePeer.objects.create(
            group=self._group,
            private_key=pair.private_key,
            public_key=pair.public_key,
            address=added.allowed_ips[0].partition("/")[0],
        )

    def _put(
        self, reached: tuple[GroupServer, RemoteHost], peer: ServicePeer, preshared_key: str
    ) -> None:
        """Put the peer on a member; a member that fails lags and gets it on its catch-up."""
        server, host = reached
        wanted = Peer(peer.public_key, (f"{peer.address}/32",), preshared_key or None)
        try:
            with self._ctx.step(_("Add the service peer on %(server)s") % {"server": server.name}):
                PeerManager(host, self._ctx).enable(wanted, PEER_NAME)
        except CatalogError as error:
            self._ctx.log(f"{server.name}: {error.message}")
            self._replicas.fall_behind(server)


def _reserved(source: str) -> list[IPv4Address]:
    """Disabled keys keep their addresses to come back with them, as WgKeys does."""
    addresses = (
        AccessKey.objects.filter(source=source, status__in=DISABLED_STATUSES)
        .exclude(client_ip=None)
        .values_list("client_ip", flat=True)
    )
    return [IPv4Address(ip) for ip in addresses]


@service_peers.register
def relay_probes(source: str) -> set[str]:
    """The service peer of the group the instance of `source` is a member of."""
    instance_id = ProtocolInstance.pk_of(source)
    if instance_id is None:
        return set()
    found = ServicePeer.objects.filter(group__members__instance_id=instance_id)
    return set(found.values_list("public_key", flat=True))
