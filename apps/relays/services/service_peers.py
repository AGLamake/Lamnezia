"""The service peer of a group on its members: the client the relay agent probes them with.

It is made on the leading server, which gives it a free address of the group, and put with that
address on the others, as a key of the group is (apps/keys/services/replicas.py). A failure on
the leader fails the operation and nobody lags; another member that cannot take the peer lags
and gets it with the leader files on its catch-up, and loses it the same way when the relay goes.
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

# A member and the connection to it.
type Reached = tuple[GroupServer, RemoteHost]


class ServicePeerPlacement:
    """Puts the service peer of a group on its members that are up to date, or takes it off.

    The leading server goes first and must succeed, as the primary of a key change does: when
    it fails, the operation fails and nobody lags, so the relay keeps the members it has.
    """

    def __init__(self, group: ServerGroup, ctx: OperationContext) -> None:
        self._group = group
        self._ctx = ctx
        self._replicas = GroupReplicas(group.pk)

    def place(self) -> None:
        """Raises RelayError when the leading server does not answer; nothing changes then."""
        if self._group.leader is None:
            return
        instance = self._group.leader.instance
        with ExitStack() as stack:
            leader, others = self._connect(stack)
            peer = ServicePeer.objects.filter(group=self._group).first()
            if peer is None:
                peer = self._create(leader, instance.source)
            else:
                self._enable(leader, peer, instance.preshared_key)
            for server in others:
                self._put(server, peer, instance.preshared_key)
        self._replicas.finish()

    def withdraw(self) -> None:
        """Take the peer off the members and forget it; a member that cannot drop it lags.

        With the leading server not answering the panel keeps the peer and touches no member:
        a catch-up takes the files of the leader, and the peer stays known to reconciliation.
        """
        peer = ServicePeer.objects.filter(group=self._group).first()
        if peer is None:
            return
        with ExitStack() as stack:
            try:
                leader, others = self._connect(stack)
            except RelayError as error:
                title = _("Keep the service peer of %(group)s") % {"group": self._group.name}
                with self._ctx.step(title):
                    self._ctx.log(error.message)
                return
            self._revoke(leader, peer)
            for server in others:
                self._drop(server, peer)
        self._replicas.finish()
        peer.delete()

    def _connect(self, stack: ExitStack) -> tuple[Reached, list[Reached]]:
        """The leading server and the other members that answer; those that do not lag.

        Raises RelayError when the leading server does not answer: then nobody lags.
        """
        with self._ctx.step(_("Connect to the servers of %(group)s") % {"group": self._group.name}):
            members = self._replicas.members()
            if not members:
                raise RelayError(RelayErrorCode.LEADER_UNREACHABLE, self._group.name)
            first, *rest = members
            try:
                leader = (first, stack.enter_context(first.connect()))
            except SshError as error:
                self._ctx.log(f"{first.name}: {error.message}")
                raise RelayError(RelayErrorCode.LEADER_UNREACHABLE, self._group.name) from error
            others: list[Reached] = []
            for server in rest:
                try:
                    others.append((server, stack.enter_context(server.connect())))
                except SshError as error:
                    self._ctx.log(f"{server.name}: {error.message}")
                    self._replicas.fall_behind(server)
        return leader, others

    def _create(self, reached: Reached, source: str) -> ServicePeer:
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

    def _enable(self, reached: Reached, peer: ServicePeer, preshared_key: str) -> None:
        server, host = reached
        wanted = Peer(peer.public_key, (f"{peer.address}/32",), preshared_key or None)
        with self._ctx.step(_("Add the service peer on %(server)s") % {"server": server.name}):
            PeerManager(host, self._ctx).enable(wanted, PEER_NAME)

    def _revoke(self, reached: Reached, peer: ServicePeer) -> None:
        server, host = reached
        with self._ctx.step(_("Remove the service peer from %(server)s") % {"server": server.name}):
            PeerManager(host, self._ctx).revoke(peer.public_key)

    def _put(self, reached: Reached, peer: ServicePeer, preshared_key: str) -> None:
        """Put the peer on a member; a member that fails lags and gets it on its catch-up."""
        try:
            self._enable(reached, peer, preshared_key)
        except CatalogError as error:
            self._fall_behind(reached[0], error.message)

    def _drop(self, reached: Reached, peer: ServicePeer) -> None:
        """Revoke the peer on a member; a member that fails lags and loses it on its catch-up."""
        try:
            self._revoke(reached, peer)
        except CatalogError as error:
            self._fall_behind(reached[0], error.message)

    def _fall_behind(self, server: GroupServer, reason: str) -> None:
        """The failed step is closed already, so the reason goes into a step of its own."""
        with self._ctx.step(_("Leave %(server)s to catch up later") % {"server": server.name}):
            self._ctx.log(reason)
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
def relay_probes(source: str) -> dict[str, str]:
    """The service peer of the group the instance of `source` is a member of, by public key."""
    instance_id = ProtocolInstance.pk_of(source)
    if instance_id is None:
        return {}
    found = ServicePeer.objects.filter(group__members__instance_id=instance_id)
    return dict(found.values_list("public_key", "address"))
