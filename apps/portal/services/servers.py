"""Server status for clients: a coarse state only, never the address or host metrics."""

from dataclasses import dataclass

from django.db import models
from django.utils.translation import gettext_lazy as _

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


def my_servers(limits: EffectiveLimits) -> list[ServerState]:
    """The servers the user's plan allows; a user without a plan has none."""
    servers = Server.objects.select_related("latest_sample")
    if limits.server_ids is not None:
        servers = servers.filter(pk__in=limits.server_ids)
    reported = servers_with_open_incident()
    return [ServerState.of(server, server.pk in reported) for server in servers]


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
