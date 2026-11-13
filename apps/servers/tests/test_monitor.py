from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from django.conf import settings
from django.utils import timezone

from apps.servers.models import Server, ServerSample
from apps.servers.services.monitor import ServerMonitor, daily_history
from apps.servers.tests.factories import ServerFactory
from apps.servers.tests.test_metrics import OUTPUT
from apps.ssh.errors import ErrorCode, SshConnectionError
from apps.ssh.services import RemoteHost
from apps.ssh.tests.fakes import TIMEOUTS, FakeRunner, result

pytestmark = pytest.mark.django_db

WEEK = timedelta(days=7)
INTERVAL = timedelta(seconds=settings.MONITOR_INTERVAL)


class FakeServers:
    """Servers answer with the given script output; the ones listed in `down` do not answer."""

    def __init__(self, output: str = OUTPUT) -> None:
        self.output = output
        self.down: set[str] = set()

    @contextmanager
    def connect(self, server: Server) -> Iterator[RemoteHost]:
        if server.name in self.down:
            raise SshConnectionError(ErrorCode.CONNECTION_TIMEOUT)
        runner = FakeRunner({"@@lamnezia-stat": result(stdout=self.output.encode())})
        yield RemoteHost(runner, TIMEOUTS)


def monitor(servers: FakeServers, at: datetime | None = None) -> ServerMonitor:
    return ServerMonitor(WEEK, servers.connect, clock=lambda: at or timezone.now())


def poll(servers: FakeServers, server: Server, at: datetime) -> ServerSample:
    server.refresh_from_db()
    return monitor(servers, at).poll(server)


def test_an_answering_server_is_online_with_its_metrics() -> None:
    server = ServerFactory()

    sample = monitor(FakeServers()).poll(server)

    assert server.online is True
    assert server.latest_sample == sample
    assert sample.memory_percent == 75.0
    assert sample.disk_percent == 25.0
    assert sample.uptime == 86461
    assert sample.stopped_containers == ["amnezia-xray", "lamnezia-probe"]
    assert sample.stopped_vpn_containers == ["amnezia-xray"]
    assert sample.cpu_percent is None


def test_cpu_load_comes_from_the_previous_reading() -> None:
    server, servers = ServerFactory(), FakeServers()
    poll(servers, server, timezone.now() - INTERVAL)
    servers.output = OUTPUT.replace("cpu  100 0 50 800", "cpu  250 0 100 850")

    assert poll(servers, server, timezone.now()).cpu_percent == 80.0


def test_one_missed_reading_keeps_the_server_online() -> None:
    server, servers = ServerFactory(), FakeServers()
    now = timezone.now()
    poll(servers, server, now - INTERVAL * 1.5)
    servers.down.add(server.name)

    sample = poll(servers, server, now - INTERVAL * 0.5)

    assert sample.error == ErrorCode.CONNECTION_TIMEOUT
    assert server.online is True


def test_a_server_down_for_two_intervals_is_offline() -> None:
    server, servers = ServerFactory(), FakeServers()
    now = timezone.now()
    poll(servers, server, now - INTERVAL * 2.5)
    servers.down.add(server.name)
    poll(servers, server, now - INTERVAL * 1.5)
    poll(servers, server, now - INTERVAL * 0.5)

    server.refresh_from_db()
    assert server.online is False


def test_a_server_never_read_has_no_status_and_one_never_answered_is_offline() -> None:
    server, servers = ServerFactory(), FakeServers()
    assert server.online is None

    servers.down.add(server.name)
    monitor(servers).poll(server)

    assert server.online is False


def test_old_readings_are_dropped() -> None:
    server = ServerFactory()
    old = ServerSample.objects.create(server=server, at=timezone.now() - WEEK - INTERVAL)
    recent = ServerSample.objects.create(server=server, at=timezone.now() - INTERVAL)

    monitor(FakeServers()).prune()

    assert list(ServerSample.objects.all()) == [recent]
    assert not ServerSample.objects.filter(pk=old.pk).exists()


def test_daily_history_sums_up_each_day() -> None:
    server, servers = ServerFactory(), FakeServers()
    now = timezone.now()
    poll(servers, server, now)
    servers.down.add(server.name)
    poll(servers, server, now)

    (today,) = daily_history(server, days=7)

    assert today.availability == 50.0
    assert today.memory == 75.0
    assert today.disk_free == 15_360_000_000


def test_the_monitor_watches_the_probe() -> None:
    server = ServerFactory(probe_port=3478)
    servers = FakeServers()

    monitor(servers).poll(server)
    assert server.probe_down is True  # lamnezia-probe exited

    servers.output = OUTPUT.replace("lamnezia-probe exited", "lamnezia-probe running")
    poll(servers, server, timezone.now())
    assert server.probe_down is False

    servers.output = OUTPUT.replace("lamnezia-probe exited\n", "")
    poll(servers, server, timezone.now())
    assert server.probe_down is True  # removed by hand


def test_the_probe_is_not_down_when_unknown_or_not_installed() -> None:
    servers = FakeServers()
    assert ServerFactory(probe_port=3478).probe_down is False  # never read

    plain = ServerFactory()
    monitor(servers).poll(plain)
    assert plain.probe_down is False

    silent = ServerFactory(name="silent", probe_port=3478)
    servers.down.add("silent")
    monitor(servers).poll(silent)
    assert silent.probe_down is False
