"""What a relay forwards: its groups with their members in order, and the digest of that.

The panel writes the result to the relay as config.json (docs/FALLBACK.md section 5); the
digest of the written file and the published ports tells a sync whether the relay got the groups
as they are now. The file holds resolved addresses, so a member name that moves to another IP
changes the digest too, and so does the agent code of a new panel version: each relay gets one
update after it. A split group also has its clone and uplink there, and the peers of its clone
(services/clone.py), its network list and its own domains (services/split_lists.py) in files of
their own, and so do the speed limits of its keys on the clone.
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
    Clone,
    RelayConfig,
    RelayGroup,
    Routing,
    SplitRouting,
    clone_interface,
    clone_peers_file,
    speed_file,
)
from amnezia_core.relay import ServicePeer as CoreServicePeer
from amnezia_core.split_lists import ListMode
from apps.groups.models import GroupMember, GroupRouting, ServerGroup
from apps.groups.services.replication import backends
from apps.protocols.services.installer import ipv4_addresses
from apps.relays.models import PeerRole, Relay, ServicePeer
from apps.relays.services.clone import clone_peers
from apps.relays.services.image import agent_digest
from apps.relays.services.split_lists import GroupList
from apps.servers.models import Server
from apps.traffic.services.speed import SpeedLimiter, clone_speed_script


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
        """The digest of `config` of this plan, of the ports the relay publishes and of the
        agent code: it changes with a port, a member, its order or address, the service peer,
        the agent settings or a panel version with another agent, which the update writes."""
        data = [sorted(self.ports), config.to_json(), agent_digest()]
        return hashlib.sha256(json.dumps(data).encode()).hexdigest()

    def config(self) -> RelayConfig:
        """The agent config; a group without members or a probe peer is left out.

        A split group without its uplink peer yet is forwarded: its clients keep working and
        the next update splits it. Raises OSError when the name of a member server does not
        resolve.
        """
        groups = []
        for group, members in self.groups:
            peers = {peer.role: peer for peer in ServicePeer.objects.filter(group=group)}
            probe, uplink = peers.get(PeerRole.PROBE), peers.get(PeerRole.UPLINK)
            if not members or probe is None:
                continue
            leader = members[0]
            split = None
            if group.routing == GroupRouting.SPLIT and uplink is not None:
                dns = tuple(map(IPv4Address, leader.instance.dns))
                split = SplitRouting(
                    _clone(leader),
                    _core_peer(uplink, leader),
                    dns=dns,
                    list_mode=ListMode(group.list_mode),
                    dns_all=group.dns_all,
                )
            groups.append(
                RelayGroup(
                    group.port,
                    tuple(map(member_backend, members)),
                    _core_peer(probe, leader),
                    Routing.SPLIT if split else Routing.FORWARD,
                    split,
                )
            )
        return RelayConfig(tuple(groups), self.check_interval, self.failures)

    def split_files(self, config: RelayConfig) -> dict[str, str]:
        """The files of each split group of `config` by name in the agent folder: the clone
        peers, the network list, the own domains and the speed limits on the clone."""
        split = {group.port for group in config.groups if group.split is not None}
        wanted = SpeedLimiter.wanted_rules() if split else {}
        files = {}
        for group, members in self.groups:
            if group.port in split:
                leader, port = members[0].instance, group.port
                files[clone_peers_file(port)] = clone_peers(leader).to_text()
                files.update(GroupList.of(group).files(port))
                files[speed_file(port)] = clone_speed_script(clone_interface(port), leader, wanted)
        return files


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


def _clone(leader: GroupMember) -> Clone:
    """The group server as the members have it: the key, the address and the parameters."""
    instance = leader.instance
    return Clone.of_server(instance.private_key, instance.subnet, instance.params)


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
