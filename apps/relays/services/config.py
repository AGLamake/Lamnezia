"""What a relay forwards: its groups with their members in order, and the digest of that.

The panel writes the result to the relay as config.json (docs/FALLBACK.md section 5); the
digest tells a sync whether the relay got the groups as they are now.
"""

import hashlib
import json
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Interface

from amnezia_core.awg import AWG_INTERFACE_PARAMS
from amnezia_core.relay import Backend, RelayConfig, RelayGroup
from amnezia_core.relay import ServicePeer as CoreServicePeer
from apps.groups.models import GroupMember, ServerGroup
from apps.groups.services.replication import backends
from apps.protocols.services.installer import ipv4_addresses
from apps.relays.models import ServicePeer
from apps.servers.models import Server


@dataclass(frozen=True)
class RelayPlan:
    """The groups of a relay server as the panel sees them now; nothing is resolved yet."""

    groups: tuple[tuple[ServerGroup, tuple[GroupMember, ...]], ...]

    @classmethod
    def of(cls, server: Server) -> "RelayPlan":
        relayed = ServerGroup.objects.filter(relay=server).order_by("port", "pk")
        return cls(tuple((group, tuple(backends(group))) for group in relayed))

    @property
    def ports(self) -> set[int]:
        return {group.port for group, _members in self.groups}

    def digest(self) -> str:
        """Changes when a group, its port, a member, its order or the service peer changes."""
        data = [
            [
                group.port,
                [[member.instance.server.host, member.instance.port] for member in members],
                _service_key(group),
            ]
            for group, members in self.groups
        ]
        return hashlib.sha256(json.dumps(data).encode()).hexdigest()

    def config(self) -> RelayConfig:
        """The agent config; a group without members or a service peer is left out.

        Raises OSError when the name of a member server does not resolve.
        """
        groups = []
        for group, members in self.groups:
            peer = ServicePeer.objects.filter(group=group).first()
            if not members or peer is None:
                continue
            groups.append(
                RelayGroup(group.port, tuple(map(_backend, members)), _core_peer(peer, members[0]))
            )
        return RelayConfig(tuple(groups))


def _service_key(group: ServerGroup) -> str:
    peer = ServicePeer.objects.filter(group=group).first()
    return peer.public_key if peer else ""


def _backend(member: GroupMember) -> Backend:
    instance = member.instance
    found = ipv4_addresses(instance.server.host)
    if not found:
        raise OSError(f"{instance.server.host} has no IPv4 address")
    return Backend(IPv4Address(found[0]), instance.port)


def _core_peer(peer: ServicePeer, leader: GroupMember) -> CoreServicePeer:
    """The service peer with the server half of the group: every member has the same."""
    instance = leader.instance
    params = {name: str(instance.params.get(name, "")) for name in AWG_INTERFACE_PARAMS}
    return CoreServicePeer(
        private_key=peer.private_key,
        address=IPv4Interface(f"{peer.address}/32"),
        server_public_key=instance.public_key,
        preshared_key=instance.preshared_key,
        awg_params={name: value for name, value in params.items() if value},
    )
