"""What runs on a server, for its card: Amnezia containers and the protocols the panel manages."""

from dataclasses import dataclass

from django.db.models import Count
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from apps.keys.models import AccessKey, KeyStatus
from apps.protocols.catalog import CATALOG, ContainerGroup
from apps.protocols.models import ProtocolInstance, ServiceInstance
from apps.protocols.services.awg_migration import MigrationState, can_migrate
from apps.protocols.services.awg_upgrade import can_upgrade
from apps.servers.models import Server

RUNNING = "running"
# The heading of the containers the panel knows nothing about, after the groups of the app.
OTHER_CONTAINERS = _("Other containers")


@dataclass(frozen=True)
class ContainerRow:
    """`state` is what docker reported in the latest monitor reading; empty if unknown."""

    name: str
    port: int | None
    instance: ProtocolInstance | None = None
    keys: int = 0
    service: ServiceInstance | None = None
    state: str = ""

    @property
    def supported(self) -> bool:
        return self.name in CATALOG

    @property
    def group(self) -> str | Promise:
        kind = CATALOG.get(self.name)
        return kind.group.label if kind else OTHER_CONTAINERS

    @property
    def description(self) -> str:
        kind = CATALOG.get(self.name)
        return str(kind.description) if kind else ""

    @property
    def stopped(self) -> bool:
        return bool(self.state) and self.state != RUNNING

    @property
    def awg_upgrade(self) -> bool:
        """The container is AmneziaWG 2.0 that the panel can move to 3.1."""
        return self.instance is not None and can_upgrade(self.instance)

    @property
    def awg_migration(self) -> MigrationState | None:
        """The move of the clients of a legacy AmneziaWG to 3.1; None for other containers."""
        if self.instance is None or not can_migrate(self.instance):
            return None
        return MigrationState.of(self.instance)


def server_containers(server: Server) -> list[ContainerRow]:
    """Containers by group: VPN protocols, services, then unknown ones.

    In each group come the containers seen by the latest import, then managed ones it no longer
    saw (e.g. stopped).
    """
    instances = {instance.container: instance for instance in server.instances.all()}
    services = {service.container: service for service in server.services.all()}
    keys = dict(
        AccessKey.objects.filter(source__in=[i.source for i in instances.values()])
        .exclude(status=KeyStatus.REVOKED)
        .values("source")
        .annotate(count=Count("id"))
        .values_list("source", "count")
    )

    states = _monitored_states(server)

    def row(name: str, port: int | None) -> ContainerRow:
        instance = instances.pop(name, None)
        count = keys.get(instance.source, 0) if instance else 0
        service = services.pop(name, None)
        return ContainerRow(name, port, instance, count, service, states.get(name, ""))

    rows = [row(seen["name"], seen["port"]) for seen in server.containers]
    rows += [row(name, instance.port) for name, instance in list(instances.items())]
    rows += [row(name, service.port) for name, service in list(services.items())]
    return sorted(rows, key=_group_order)


def _group_order(row: ContainerRow) -> int:
    groups = list(ContainerGroup)
    kind = CATALOG.get(row.name)
    return groups.index(kind.group) if kind else len(groups)


def _monitored_states(server: Server) -> dict[str, str]:
    """Container states of the latest reading; none when the server did not answer it."""
    sample = server.latest_sample
    if sample is None or not sample.answered:
        return {}
    return {container["name"]: container["state"] for container in sample.containers}
