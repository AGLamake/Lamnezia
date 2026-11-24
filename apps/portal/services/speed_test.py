"""The port speed test for portal users: a server they have a key on, the ports the admin allows.

The browser test of the administrator (apps/protocols/services/speed_test.py) with limits: the
setting turns it on, only the ports from the settings that are free on the server, ten minutes,
a few starts an hour for one user and one server, and never while another test of the server
runs or starts. The user's address is not written down; the journal keeps who started which test.
"""

from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import QuerySet

from apps.accounts.models import User
from apps.core.services.site_settings import cached_settings
from apps.groups.models import GroupMember
from apps.operations.models import Operation, OperationStatus
from apps.portal.services.keys import MyKeys
from apps.protocols.services import speed_test
from apps.protocols.services.installation import used_ports
from apps.protocols.services.speed_test import (
    RunningTest,
    SpeedTestMode,
    SpeedTestPlan,
    browser_reachable,
)
from apps.servers.models import Server

MINUTES = 10
STARTS_PER_WINDOW = 3
WINDOW = timedelta(hours=1)
MAX_PORTS = speed_test.MAX_PORTS
_KINDS = (speed_test.START_KIND, speed_test.STOP_KIND)
_UNFINISHED = (OperationStatus.PENDING, OperationStatus.RUNNING)


class SpeedTestRefusedError(Exception):
    """The test cannot start now; the message says why to the user."""


class TooManyStartsError(SpeedTestRefusedError):
    pass


class ServerBusyError(SpeedTestRefusedError):
    pass


class UnavailableError(SpeedTestRefusedError):
    pass


class MySpeedTest:
    """The speed tests one portal user may run and the state of their test on a server."""

    def __init__(self, user: User, keys: MyKeys) -> None:
        self._user = user
        self._keys = keys

    @staticmethod
    def enabled() -> bool:
        site = cached_settings.get()
        return site.portal_speed_test and bool(site.speed_test_ports)

    def servers(self) -> list[Server]:
        """Servers of the user's keys the browser can reach at the address in the key.

        A server of a group is left out: its keys lead to the group, not to the server, and the
        page would give away which server leads it.
        """
        if not (self.enabled() and self._user.can_have_keys):
            return []
        hosts = {
            instance.server_id: instance.host
            for instance in self._keys.keyed_instances()
            if instance.server_id is not None
        }
        grouped = GroupMember.objects.filter(instance__server_id__in=hosts)
        servers = Server.objects.filter(pk__in=hosts).exclude(
            pk__in=grouped.values("instance__server_id")
        )
        return [
            server
            for server in servers.order_by("name")
            if hosts[server.pk] == server.host and browser_reachable(server.host)
        ]

    def server(self, pk: int) -> Server | None:
        return next((server for server in self.servers() if server.pk == pk), None)

    @staticmethod
    def ports(server: Server) -> tuple[int, ...]:
        """The allowed ports nothing on the server takes over UDP."""
        taken = used_ports(server)
        allowed = cached_settings.get().speed_test_ports
        return tuple(port for port in allowed if (port, "udp") not in taken)

    def running(self, server: Server, now: datetime) -> RunningTest | None:
        """The user's own test of the server while it runs."""
        test = RunningTest.of(server, now)
        return test if test and test.started_by == self._user.pk else None

    def starting(self, server: Server) -> bool:
        """The user's test is waiting or being started."""
        return self._unfinished(server).filter(created_by=self._user).exists()

    def failed(self, server: Server, now: datetime) -> bool:
        """The user's latest start of the server failed within the last window."""
        latest = _operations(server).order_by("-created_at").first()
        return bool(
            latest
            and latest.created_by_id == self._user.pk
            and latest.kind == speed_test.START_KIND
            and latest.status == OperationStatus.FAILED
            and latest.created_at > now - WINDOW
        )

    def busy(self, server: Server, now: datetime) -> bool:
        """Another test of the server runs or starts, the user's own or anyone else's."""
        return RunningTest.of(server, now) is not None or self._unfinished(server).exists()

    def next_start(self, server: Server, now: datetime) -> datetime | None:
        """When the user may start again, or None if they may now."""
        starts = list(
            self._starts(server)
            .filter(created_at__gt=now - WINDOW)
            .order_by("-created_at")
            .values_list("created_at", flat=True)[:STARTS_PER_WINDOW]
        )
        return starts[-1] + WINDOW if len(starts) >= STARTS_PER_WINDOW else None

    def start(self, server: Server, ports: tuple[int, ...], now: datetime) -> Operation:
        """Start the test; the server row lock makes two starts at once wait for each other."""
        with transaction.atomic():
            Server.objects.select_for_update().get(pk=server.pk)
            if self.server(server.pk) is None:
                raise UnavailableError
            if not 1 <= len(ports) <= MAX_PORTS or not set(ports) <= set(self.ports(server)):
                raise UnavailableError
            if self.next_start(server, now) is not None:
                raise TooManyStartsError
            if self.busy(server, now):
                raise ServerBusyError
            plan = SpeedTestPlan.new(ports, MINUTES, SpeedTestMode.BROWSER)
            return speed_test.start(server, plan, self._user)

    def _starts(self, server: Server) -> QuerySet[Operation]:
        return _operations(server).filter(kind=speed_test.START_KIND, created_by=self._user)

    @staticmethod
    def _unfinished(server: Server) -> QuerySet[Operation]:
        return _operations(server).filter(status__in=_UNFINISHED)


def _operations(server: Server) -> QuerySet[Operation]:
    return Operation.objects.filter(kind__in=_KINDS, params__server_id=server.pk)
