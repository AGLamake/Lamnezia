"""Ports of new containers: the free low port of AmneziaWG and the iperf3 speed test by port."""

import shlex
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone, translation

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.operations.models import Operation, OperationStatus
from apps.protocols.errors import SetupErrorCode
from apps.protocols.models import InstanceOrigin, ProtocolInstance
from apps.protocols.services import speed_test
from apps.protocols.services.awg_installer import (
    AwgInstaller,
    AwgLegacyInstaller,
    WireGuardInstaller,
)
from apps.protocols.services.installation import used_ports
from apps.protocols.services.openvpn_installer import OpenVpnInstaller
from apps.protocols.services.ports import LOW_PORTS, free_ports, is_high, ports_page_url
from apps.protocols.services.speed_test import RunningTest, SpeedTestPlan
from apps.protocols.services.xray_installer import XrayInstaller
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import CommandResult, CommandRunner, RemoteHost, RemoteProcess
from apps.ssh.tests.fakes import TIMEOUTS, FakeProcess, result

type OnCommit = Callable[..., Any]


def test_high_port_is_above_9999() -> None:
    assert not is_high(9999)
    assert is_high(10000)
    assert not is_high(None)


def test_free_ports_skip_a_port_taken_for_any_transport() -> None:
    taken = {(5542, "udp"), (5543, "tcp")}

    assert free_ports(LOW_PORTS, taken, ("udp",)) == [5543]
    assert free_ports(LOW_PORTS, taken, ("tcp", "udp"), count=2) == [5544, 5545]
    assert free_ports([5542], taken, ("udp",)) == []


def test_low_ports_stay_up_to_9999_and_above_the_system_ports() -> None:
    assert LOW_PORTS[0] == 5542
    assert max(LOW_PORTS) == 9999
    assert min(LOW_PORTS) == 1024
    assert len(set(LOW_PORTS)) == len(LOW_PORTS)


def test_amneziawg_suggests_the_first_free_low_port() -> None:
    assert AwgInstaller.suggested_port(set()) == 5542
    assert AwgLegacyInstaller.suggested_port({(5542, "udp")}) == 5543
    # A TCP port does not take the UDP one.
    assert AwgInstaller.suggested_port({(5542, "tcp")}) == 5542


def test_other_protocols_and_a_full_server_keep_the_app_port() -> None:
    all_low = {(port, "udp") for port in LOW_PORTS}

    assert WireGuardInstaller.suggested_port(set()) == 51820
    assert AwgInstaller.suggested_port(all_low) == 55424


@pytest.mark.django_db
def test_an_imported_openvpn_on_tcp_takes_the_tcp_port() -> None:
    server = ServerFactory(containers=[])
    ProtocolInstance.objects.create(
        server=server, container="amnezia-openvpn", port=1194, subnet="10.8.0.0/24",
        params={"transport": "tcp"}, origin=InstanceOrigin.IMPORTED,
    )  # fmt: skip

    taken = used_ports(server)

    assert (1194, "tcp") in taken and (1194, "udp") not in taken
    assert OpenVpnInstaller.ports_of(1194, {}) == {(1194, "udp")}


def test_only_wireguard_gets_a_low_port_advice() -> None:
    assert WireGuardInstaller.low_port_advice({(5542, "udp")}) == 5543
    # AmneziaWG takes a low port by itself; XRay is TCP and OpenVPN is low already.
    assert AwgInstaller.low_port_advice(set()) is None
    assert XrayInstaller.low_port_advice(set()) is None
    assert OpenVpnInstaller.low_port_advice(set()) is None
    assert WireGuardInstaller.low_port_advice({(port, "udp") for port in LOW_PORTS}) is None


def test_wiki_page_follows_the_language() -> None:
    with translation.override("ru"):
        assert ports_page_url().endswith("/wiki/ru-Ports")
    with translation.override("en"):
        assert ports_page_url().endswith("/wiki/Ports")


def test_speed_test_container_is_unprivileged_and_temporary() -> None:
    arguments = SpeedTestPlan((5542, 55424), minutes=5).run_arguments()

    for flag in ("--rm", "--read-only", "--log-driver", "--cap-drop"):
        assert flag in arguments
    published = [arguments[i + 1] for i, value in enumerate(arguments) if value == "-p"]
    assert published == ["5542:5542/tcp", "5542:5542/udp", "55424:55424/tcp", "55424:55424/udp"]
    assert arguments[-3:] == ["300", "5542", "55424"]


@dataclass
class FakeDockerHost(CommandRunner):
    """Answers the docker commands of the speed test; `run_error` makes `docker run` fail."""

    run_error: bytes = b""
    commands: list[str] = field(default_factory=list)
    dockerfile: bytes = b""
    locks: int = 0

    def host(self) -> RemoteHost:
        return RemoteHost(self, TIMEOUTS)

    def run(
        self, command: str, *, stdin: bytes = b"", timeout: float | None = None
    ) -> CommandResult:
        if command == "id -u":
            return result(stdout=b"0\n")
        script = shlex.split(command)[2]
        self.commands.append(script)
        if script.startswith("docker build"):
            self.dockerfile = stdin
        if script.startswith("docker run") and self.run_error:
            return result(125, stderr=self.run_error)
        return result()

    def start(self, command: str) -> RemoteProcess:
        self.locks += 1
        return FakeProcess(["lamnezia-locked"])


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> FakeDockerHost:
    fake = FakeDockerHost()

    @contextmanager
    def connect(_server: Server) -> Iterator[RemoteHost]:
        yield fake.host()

    monkeypatch.setattr("apps.protocols.operations.connect", connect)
    return fake


@pytest.mark.django_db
def test_speed_test_page_offers_a_low_and_a_high_free_port(admin_client: Client) -> None:
    server = ServerFactory(containers=[{"name": "amnezia-awg2", "port": 55424, "transport": "udp"}])

    page = admin_client.get(f"/protocols/speed-test/{server.pk}/")

    assert 'value="5542, 55425"' in page.content.decode()


@pytest.mark.django_db
def test_speed_test_starts_iperf3_and_shows_the_client_commands(
    admin_client: Client, docker: FakeDockerHost, django_capture_on_commit_callbacks: OnCommit
) -> None:
    server = ServerFactory(host="vpn.example.com")
    url = f"/protocols/speed-test/{server.pk}/"

    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(url, {"ports": "5542, 55424", "minutes": "10"})

    operation = Operation.objects.get()
    assert operation.status == OperationStatus.SUCCEEDED
    assert operation.params == {"server_id": server.pk, "ports": [5542, 55424], "minutes": 10}
    assert b"iperf3" in docker.dockerfile
    assert docker.commands[-1].startswith("docker run")
    assert docker.locks == 1
    assert AuditEvent.objects.get(action=speed_test.START_KIND).data["ports"] == [5542, 55424]
    html = admin_client.get(url).content.decode()
    assert "iperf3 -c vpn.example.com -p 55424 -u -b 200M -t 10 -R" in html


@pytest.mark.django_db
def test_speed_test_stop_removes_the_container(
    admin_client: Client, docker: FakeDockerHost, django_capture_on_commit_callbacks: OnCommit
) -> None:
    server = ServerFactory()

    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(f"/protocols/speed-test/{server.pk}/", {"ports": "5542", "minutes": "5"})
        admin_client.post(f"/protocols/speed-test/{server.pk}/stop/")

    assert docker.commands[-1] == f"docker rm -f {speed_test.CONTAINER}"
    assert RunningTest.of(server, timezone.now()) is None


@pytest.mark.django_db
def test_speed_test_tells_a_busy_port_apart(
    docker: FakeDockerHost, django_capture_on_commit_callbacks: OnCommit
) -> None:
    server = ServerFactory()
    docker.run_error = b"Error: listen udp 0.0.0.0:5542: bind: address already in use"

    with django_capture_on_commit_callbacks(execute=True):
        operation = speed_test.start(server, SpeedTestPlan((5542,)), UserFactory(role=Role.ADMIN))

    operation.refresh_from_db()
    assert operation.status == OperationStatus.FAILED
    assert operation.error_code == SetupErrorCode.SPEED_TEST_PORT_BUSY
    assert RunningTest.of(server, timezone.now()) is None


@pytest.mark.django_db
def test_running_test_ends_after_its_minutes() -> None:
    server = ServerFactory()
    operation = Operation.objects.create(
        kind=speed_test.START_KIND,
        params={"server_id": server.pk, "ports": [5542], "minutes": 10},
        status=OperationStatus.SUCCEEDED,
        finished_at=timezone.now(),
    )
    assert operation.finished_at

    assert RunningTest.of(server, operation.finished_at + timedelta(minutes=9))
    assert RunningTest.of(server, operation.finished_at + timedelta(minutes=11)) is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("ports", "error"),
    [
        ("abc", "Enter port numbers"),
        ("1, 2, 3, 4, 5", "from one to four"),
        ("22", "Already used"),
        ("55424", "Already used"),
    ],
)
def test_speed_test_refuses_bad_or_taken_ports(
    admin_client: Client, ports: str, error: str
) -> None:
    server = ServerFactory(
        port=22, containers=[{"name": "amnezia-awg2", "port": 55424, "transport": "udp"}]
    )

    response = admin_client.post(
        f"/protocols/speed-test/{server.pk}/", {"ports": ports, "minutes": "10"}
    )

    assert error in response.content.decode()
    assert not Operation.objects.exists()


@pytest.mark.django_db
def test_speed_test_is_for_admins_only(client: Client) -> None:
    server = ServerFactory()
    client.force_login(UserFactory(role=Role.MODERATOR))

    assert client.get(f"/protocols/speed-test/{server.pk}/").status_code == 403
    assert client.post(f"/protocols/speed-test/{server.pk}/stop/").status_code == 403
