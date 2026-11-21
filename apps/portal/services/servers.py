"""Server status for clients: a coarse state only, never the address or host metrics.

A server group is one line under its own name: clients reach it as one server, and the names
of its members are not shown.
"""

from collections import defaultdict
from dataclasses import dataclass

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.groups.models import GroupMember, ServerGroup
from apps.plans.services.limits import EffectiveLimits
from apps.servers.models import Server, ServerStatus
from apps.support.services.incidents import servers_with_open_incident


class Health(models.TextChoices):
    WORKING = "working", _("working")
    PROBLEMS = "problems", _("problems")
    MAINTENANCE = "maintenance", _("maintenance")
    UNKNOWN = "unknown", _("no data yet")


@dataclass(frozen=True)
class ServerState:
    """`pk` only lets the user report the server; it says nothing of where the server is."""

    pk: int
    name: str
    location: str
    health: Health

    @classmethod
    def of(cls, server: Server, incident: bool = False) -> "ServerState":
        """`incident`: users reported the server and the staff have not closed it yet."""
        health = Health.PROBLEMS if incident and not server.maintenance else _health(server)
        return cls(server.pk, server.name, server.location, health)

    @classmethod
    def of_group(
        cls, group: ServerGroup, members: list[Server], reported: set[int]
    ) -> "ServerState":
        """`members`: the servers of the group the plan allows, the leading one first; a report
        goes to it. The group works while its relay or one of them answers."""
        health = _group_health(group, members)
        if health != Health.MAINTENANCE and any(server.pk in reported for server in members):
            health = Health.PROBLEMS
        return cls(members[0].pk, group.name, "", health)


def my_servers(limits: EffectiveLimits) -> list[ServerState]:
    """The servers the user's plan allows, a group as one of them; a user without a plan has
    none."""
    servers = Server.objects.select_related("latest_sample")
    if limits.server_ids is not None:
        servers = servers.filter(pk__in=limits.server_ids)
    reported = servers_with_open_incident()
    groups = _groups_of(list(servers))
    in_groups = {server.pk for members in groups.values() for server in members}
    standalone = [
        ServerState.of(server, server.pk in reported)
        for server in servers
        if server.pk not in in_groups
    ]
    grouped = [ServerState.of_group(group, members, reported) for group, members in groups.items()]
    return standalone + grouped


def _groups_of(servers: list[Server]) -> dict[ServerGroup, list[Server]]:
    """The groups among `servers`, by name, each with its servers, the leading one first."""
    by_pk = {server.pk: server for server in servers}
    members = (
        GroupMember.objects.filter(instance__server__in=by_pk)
        .select_related("group__relay__latest_sample", "instance")
        .order_by("group__name", "-leading", "priority", "pk")
    )
    groups: dict[ServerGroup, list[Server]] = defaultdict(list)
    for member in members:
        server = by_pk[member.instance.server_id]
        if server not in groups[member.group]:
            groups[member.group].append(server)
    return groups


def _group_health(group: ServerGroup, members: list[Server]) -> Health:
    healths = {_health(server) for server in members}
    if group.relay is not None and _health(group.relay) == Health.WORKING:
        healths.add(Health.WORKING)
    for health in (Health.WORKING, Health.PROBLEMS, Health.UNKNOWN):
        if health in healths:
            return health
    return Health.MAINTENANCE


def _health(server: Server) -> Health:
    if server.maintenance:
        return Health.MAINTENANCE
    online = server.online
    if server.status == ServerStatus.FAILED or online is False:
        return Health.PROBLEMS
    if online is None:
        return Health.UNKNOWN
    sample = server.latest_sample
    if sample is not None and sample.stopped_vpn_containers:
        return Health.PROBLEMS
    return Health.WORKING
