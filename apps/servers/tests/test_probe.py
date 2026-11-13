import binascii
import os
import socket
import threading
import time

import pytest
from django.test import Client
from django.utils.timezone import now

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.tests.factories import OperationFactory
from apps.servers.forms import ProbeForm
from apps.servers.models import Server, ServerSample
from apps.servers.probe import stun_responder
from apps.servers.services import probe
from apps.servers.services.probe import ProbeError, ProbeErrorCode, ProbeSetup, StunPing
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import RemoteHost
from apps.ssh.tests.fakes import TIMEOUTS, FakeRunner, result

TRANSACTION = bytes(range(12))


class StubPing(StunPing):
    def __init__(self, answers: bool) -> None:
        self.answers = answers
        self.asked: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> bool:
        self.asked.append((host, port))
        return self.answers


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


# --- STUN messages ---


def test_a_binding_request_gets_the_address_it_came_from() -> None:
    request = stun_responder.binding_request(TRANSACTION)

    for host in ("198.51.100.7", "2001:db8::42"):
        response = stun_responder.binding_response(request, host, 54321)
        assert response is not None
        assert stun_responder.mapped_address(response, TRANSACTION) == (host, 54321)


def test_the_response_carries_a_valid_fingerprint() -> None:
    response = stun_responder.binding_response(
        stun_responder.binding_request(TRANSACTION), "198.51.100.7", 1
    )
    assert response is not None

    body, fingerprint = response[:-8], response[-4:]
    expected = binascii.crc32(body) ^ 0x5354554E
    assert int.from_bytes(fingerprint) == expected
    assert int.from_bytes(response[2:4]) == len(response) - 20


@pytest.mark.parametrize(
    "packet",
    [
        b"",
        b"\x00" * 19,
        stun_responder.binding_request(TRANSACTION)[:4] + b"\x00\x00\x00\x00" + TRANSACTION,
        b"\x00\x03" + stun_responder.binding_request(TRANSACTION)[2:],
        stun_responder.binding_request(TRANSACTION) + b"\x00" * 4,
        b"\x00" * 600,
    ],
    ids=["empty", "short", "no-cookie", "not-binding", "bad-length", "too-long"],
)
def test_anything_but_a_binding_request_is_ignored(packet: bytes) -> None:
    assert stun_responder.binding_response(packet, "198.51.100.7", 1) is None


def test_a_response_to_another_request_is_not_ours() -> None:
    response = stun_responder.binding_response(
        stun_responder.binding_request(TRANSACTION), "198.51.100.7", 1
    )
    assert response is not None

    assert stun_responder.mapped_address(response, os.urandom(12)) is None


def test_ping_gets_an_answer_from_a_running_responder() -> None:
    port = free_udp_port()
    threading.Thread(target=stun_responder.serve, args=(port,), daemon=True).start()
    ping = StunPing(attempts=1, timeout=0.5)

    # Until the thread binds the port, Windows answers a ping at once with "port unreachable":
    # keep asking for a while instead of a fixed number of attempts.
    deadline = time.monotonic() + 5
    while not ping("127.0.0.1", port) and time.monotonic() < deadline:
        time.sleep(0.05)

    assert ping("127.0.0.1", port)


def test_ping_without_a_responder_gives_up() -> None:
    assert not StunPing(attempts=2, timeout=0.1)("127.0.0.1", free_udp_port())


# --- Installing and removing ---


@pytest.mark.django_db
def test_install_starts_a_locked_down_container_and_saves_the_port() -> None:
    server = ServerFactory(probe_port=3478)
    runner, ping = FakeRunner(), StubPing(answers=True)

    setup(server, runner, ping).install(3479)

    commands = [command for command, _stdin in runner.calls]
    assert any("/opt/lamnezia/probe/stun_responder.py" in c for c in commands)
    uploaded = next(stdin for command, stdin in runner.calls if "stun_responder.py" in command)
    assert uploaded == probe._SCRIPT.read_bytes()
    run = next(c for c in commands if "docker run" in c)
    for option in ("--log-driver none", "--read-only", "--cap-drop ALL", "3479:3479/udp"):
        assert option in run
    assert runner.started, "the server lock is taken"
    assert ping.asked == [(server.host, 3479)]
    server.refresh_from_db()
    assert server.probe_port == 3479


@pytest.mark.django_db
def test_a_probe_that_does_not_answer_is_removed_and_not_given_out() -> None:
    server = ServerFactory(probe_port=3478)
    runner = FakeRunner()

    with pytest.raises(ProbeError) as error:
        setup(server, runner, StubPing(answers=False)).install(3478)

    assert error.value.code == ProbeErrorCode.NO_ANSWER
    assert "firewall" in error.value.hint
    assert "docker rm -f lamnezia-probe" in runner.last_command
    server.refresh_from_db()
    assert server.probe_port is None


@pytest.mark.django_db
def test_a_busy_port_is_reported() -> None:
    server = ServerFactory()
    busy = result(125, stderr=b"Error starting userland proxy: port is already allocated")
    runner = FakeRunner({"docker run": busy})

    with pytest.raises(ProbeError) as error:
        setup(server, runner, StubPing(answers=True)).install(3478)

    assert error.value.code == ProbeErrorCode.PORT_BUSY
    assert server.probe_port is None


@pytest.mark.django_db
def test_remove_stops_giving_out_the_probe_first() -> None:
    server = ServerFactory(probe_port=3478)
    runner = FakeRunner()

    setup(server, runner, StubPing(answers=True)).remove()

    commands = [command for command, _stdin in runner.calls]
    assert any("docker rm -f lamnezia-probe" in c for c in commands)
    assert any("rm -rf -- /opt/lamnezia/probe" in c for c in commands)
    server.refresh_from_db()
    assert server.probe_port is None


def setup(server: Server, runner: FakeRunner, ping: StunPing) -> ProbeSetup:
    ctx = OperationContext(OperationFactory())
    return ProbeSetup(server, RemoteHost(runner, TIMEOUTS), ctx, ping)


# --- Admin pages ---


@pytest.fixture
def admin_client(client: Client) -> Client:
    client.force_login(UserFactory(role=Role.ADMIN))
    return client


@pytest.mark.django_db
def test_admin_installs_and_removes_the_probe(admin_client: Client) -> None:
    server = ServerFactory(probe_port=3478)

    page = admin_client.get(f"/servers/{server.pk}/").content.decode()
    assert "Reachability probe" in page
    assert "UDP port 3478" in page

    admin_client.post(f"/servers/{server.pk}/probe/", {"port": "3479"})
    admin_client.post(f"/servers/{server.pk}/probe/remove/")

    kinds = list(Operation.objects.order_by("pk").values_list("kind", "params"))
    assert kinds == [
        (probe.INSTALL_KIND, {"server_id": server.pk, "port": 3479}),
        (probe.REMOVE_KIND, {"server_id": server.pk}),
    ]


@pytest.mark.django_db
def test_the_card_warns_when_the_monitor_finds_no_probe(admin_client: Client) -> None:
    server = ServerFactory(probe_port=3478)
    warning = "did not find the probe running"
    assert warning not in admin_client.get(f"/servers/{server.pk}/").content.decode()

    containers = [{"name": "amnezia-awg2", "state": "running"}]
    sample = ServerSample.objects.create(server=server, at=now(), containers=containers)
    server.record_sample(sample)

    assert warning in admin_client.get(f"/servers/{server.pk}/").content.decode()


@pytest.mark.django_db
def test_clients_cannot_manage_the_probe(client: Client) -> None:
    server = ServerFactory()
    client.force_login(UserFactory())

    response = client.post(f"/servers/{server.pk}/probe/", {"port": "3478"})

    assert response.status_code in (302, 403)
    assert not Operation.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("port", ["22", "1023", "55424", "70000"])
def test_the_probe_port_must_be_free_and_unprivileged(port: str) -> None:
    server = ServerFactory(containers=[{"name": "amnezia-awg2", "port": "55424"}])

    assert not ProbeForm({"port": port}, server=server).is_valid()
    assert ProbeForm({"port": "3478"}, server=server).is_valid()


def test_the_responder_uses_only_the_standard_library() -> None:
    """The file runs alone on the server: only the standard library."""
    source = probe._SCRIPT.read_text(encoding="utf-8")
    assert "django" not in source
    assert "apps." not in source
