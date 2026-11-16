"""What a relay forwards: its groups with their members in order, and the digest of that.

The panel writes the result to the relay as config.json (docs/FALLBACK.md section 5); the
digest of the written file and the published ports tells a sync whether the relay got the groups
as they are now. The file holds resolved addresses, so a member name that moves to another IP
changes the digest too.
"""

import hashlib
import json
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Interface

from amnezia_core.awg import AWG_INTERFACE_PARAMS
from amnezia_core.relay import (
    DEFAULT_CHECK_INTERVAL,
    DEFAULT_FAILURES,
    Backend,
    RelayConfig,
    RelayGroup,
)
from amnezia_core.relay import ServicePeer as CoreServicePeer
from apps.groups.models import GroupMember, ServerGroup
from apps.groups.services.replication import backends
from apps.protocols.services.installer import ipv4_addresses
from apps.relays.models import Relay, ServicePeer
from apps.servers.models import Server


@dataclass(frozen=True)
class RelayPlan:
    """The groups of a relay server and its agent settings as the panel sees them now; nothing
    is resolved yet."""

    groups: tuple[tuple[ServerGroup, tuple[GroupMember, ...]], ...]
    check_interval: int = DEFAULT_CHECK_INTERVAL
    failures: int = DEFAULT_FAILURES

    @classmethod
    def of(cls, server: Server) -> "RelayPlan":
        relayed = ServerGroup.objects.filter(relay=server).order_by("port", "pk")
        groups = tuple((group, tuple(backends(group))) for group in relayed)
        relay = Relay.objects.filter(server=server).first()
        if relay is None:
            return cls(groups)
        return cls(groups, relay.check_interval, relay.failures)

    @property
    def ports(self) -> set[int]:
        return {group.port for group, _members in self.groups}

    def digest(self, config: RelayConfig) -> str:
        """The digest of `config` of this plan and of the ports the relay publishes: it changes
        with a port, a member, its order or address, the service peer or the agent settings."""
        data = [sorted(self.ports), config.to_json()]
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
                RelayGroup(
                    group.port, tuple(map(member_backend, members)), _core_peer(peer, members[0])
                )
            )
        return RelayConfig(tuple(groups), self.check_interval, self.failures)


def members_on_relay(server: Server) -> list[GroupMember]:
    """Members of the groups of the relay server that run on that server.

    Such a member listens on the port of its group, the port the relay must publish: the relay
    cannot forward to it, by the public address or any other.
    """
    found = GroupMember.objects.filter(group__relay=server, instance__server=server)
    return list(found.select_related("group", "instance"))


def member_backend(member: GroupMember) -> Backend:
    """The address the relay forwards to: the first IPv4 address of the server; OSError if none."""
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
