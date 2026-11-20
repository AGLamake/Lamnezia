"""Lost VPN interfaces: a running WireGuard-family container without its interface is restarted.

The kernel may kill amneziawg-go when memory runs out: docker still reports the container
running, but clients have no connection. After `readings` answered readings in a row without the
interface the monitor launches a restart operation (it takes the server lock, DESIGN.md 5.4),
with a pause that doubles after each one, up to `attempts` restarts; then administrators only
hear that it did not help. Nothing is restarted while an operation on the server is unfinished,
and a container that is not running (stopped by the administrator) ends the watch untold.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import Q

from amnezia_core.containers import WG_CONTAINERS
from apps.notifications.events import Event
from apps.notifications.services.delivery import emit
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.launcher import launch
from apps.servers.models import InterfaceRepair, Server, ServerSample

RESTART_KIND = "servers.restart_lost_interface"
_UNFINISHED = (OperationStatus.PENDING, OperationStatus.RUNNING)


@dataclass(frozen=True)
class InterfaceRules:
    """Readings in a row before the first restart, the restarts at most and the first pause."""

    readings: int
    attempts: int
    pause: timedelta

    def pause_after(self, attempt: int) -> timedelta:
        """The pause after restart number `attempt`, counted from 1: 1, 2, 4... times `pause`."""
        return self.pause * (1 << (attempt - 1))


INTERFACE_RULES = InterfaceRules(readings=3, attempts=3, pause=timedelta(minutes=5))


class InterfaceWatch:
    """Keeps the `InterfaceRepair` rows of a server in step with its latest readings."""

    def __init__(self, rules: InterfaceRules) -> None:
        self._rules = rules

    def check(self, server: Server, now: datetime) -> None:
        if server_busy(server):
            return
        recent = list(
            server.samples.filter(error="", at__lte=now).order_by("-at")[: self._rules.readings]
        )
        if not recent:
            return
        repairs = {repair.container: repair for repair in server.interface_repairs.all()}
        for name in WG_CONTAINERS:
            repair = repairs.get(name)
            if name in recent[0].missing_interfaces:
                self._missing(server, name, repair, recent, now)
            elif repair is not None:
                self._ended(repair, recent[0])

    def _missing(
        self,
        server: Server,
        name: str,
        repair: InterfaceRepair | None,
        recent: Sequence[ServerSample],
        now: datetime,
    ) -> None:
        if repair is None:
            if len(recent) < self._rules.readings:
                return
            if not all(name in sample.missing_interfaces for sample in recent):
                return
            repair = InterfaceRepair(server=server, container=name, missing_since=recent[-1].at)
            _tell(Event.INTERFACE_LOST, repair)
        if repair.next_attempt_at is not None and now < repair.next_attempt_at:
            repair.save()
            return
        if repair.attempts < self._rules.attempts:
            self._restart(repair, now)
        elif not repair.gave_up:
            repair.gave_up = True
            _tell(Event.INTERFACE_REPAIR_FAILED, repair)
        repair.save()

    def _restart(self, repair: InterfaceRepair, now: datetime) -> None:
        repair.attempts += 1
        repair.next_attempt_at = now + self._rules.pause_after(repair.attempts)
        server = repair.server
        launch(
            RESTART_KIND,
            {"server_id": server.pk, "container": repair.container},
            subject=f"{server} · {repair.container}",
        )

    def _ended(self, repair: InterfaceRepair, latest: ServerSample) -> None:
        """The interface is back, or the container stopped or went away: the latter is untold."""
        running = any(
            c["name"] == repair.container and c["state"] == "running" for c in latest.containers
        )
        if running:
            _tell(Event.INTERFACE_RESTORED, repair)
        repair.delete()


def server_busy(server: Server) -> bool:
    """An operation on the server, one of its protocols or services is not finished yet."""
    on_server = (
        Q(params__server_id=server.pk)
        | Q(params__instance_id__in=list(server.instances.values_list("pk", flat=True)))
        | Q(params__service_id__in=list(server.services.values_list("pk", flat=True)))
    )
    return Operation.objects.filter(on_server, status__in=_UNFINISHED).exists()


def _tell(event: Event, repair: InterfaceRepair) -> None:
    server = repair.server
    since = repair.missing_since.isoformat()
    emit(
        event,
        {
            "server": server.name,
            "host": server.host,
            "container": repair.container,
            "since": since,
            "attempts": repair.attempts,
        },
        key=f"{event}:{server.pk}:{repair.container}:{since}",
    )
