import shlex
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from django.test import Client

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.context import OperationContext
from apps.operations.tests.factories import OperationFactory
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.models import InstanceOrigin, ProtocolInstance
from apps.protocols.services.awg_installer import AwgInstaller
from apps.protocols.services.container import ProtocolContainer, Transport
from apps.protocols.services.installation import INSTALL_KIND, InstallRequest, start_install
from apps.protocols.services.installer import ContainerInstaller, InstallerRegistry, installers
from apps.protocols.tests.fake_awg import FakeAwgServer
from apps.protocols.tests.fakes import FakeDebianServer
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import CommandResult, RemoteHost
from apps.ssh.tests.fakes import result

pytestmark = pytest.mark.django_db

OnCommit = Callable[..., AbstractContextManager[Any]]
DUMMY = "amnezia-dummy"
_DUMMY_COMMANDS = (
    "lsof ",
    "docker ps",
    "docker build",
    "docker run",
    "docker inspect",
    "docker network connect",
    "docker start",
    "docker exec",
)


class DummyInstaller(ContainerInstaller[str]):
    """A service the panel does not have: proves that a new one needs only a class."""

    container = DUMMY
    title = "Dummy"
    folder = "dummy"
    transport = Transport.TCP
    default_port = 1080
    conntrack = True
    data_volume = True

    def run_arguments(self) -> list[str]:
        port = f"{self.port}:{self.port}/tcp"
        return ["docker", "run", "-d", "-p", port, "--name", DUMMY, DUMMY]

    def configure(self) -> str:
        self._docker.shell.run(self.script("configure_container.sh"))
        return "configured"


@dataclass
class FakeDummyServer(FakeDebianServer):
    """A ready host that runs the docker commands of DummyInstaller."""

    commands: set[str] = field(default_factory=lambda: {"docker", "fuser", "lsof"})
    networks: set[str] = field(default_factory=lambda: {"bridge", "amnezia-dns-net"})
    state: str = ""
    has_conntrack: bool = False
    port_users: str = ""
    volume_left: str = ""
    files: dict[str, bytes] = field(default_factory=dict)
    scripts: list[str] = field(default_factory=list)

    def run(
        self, command: str, *, stdin: bytes = b"", timeout: float | None = None
    ) -> CommandResult:
        argv = shlex.split(command)
        if argv[:2] != ["sh", "-c"]:
            return super().run(command, stdin=stdin, timeout=timeout)
        script = argv[2]
        if "command -v conntrack" in script:
            return result(0 if self.has_conntrack else 1)
        if "apt-get" in script and "conntrack" in script:
            self.has_conntrack = True
            return result()
        if "target=" in script:
            self.files[shlex.split(script.split("target=", 1)[1].split("\n", 1)[0])[0]] = stdin
            return result()
        if not script.startswith(_DUMMY_COMMANDS) and "docker rm -fv" not in script:
            return super().run(command, stdin=stdin, timeout=timeout)
        self.scripts.append(script)
        return self._docker(script)

    def _docker(self, script: str) -> CommandResult:
        if script.startswith("lsof "):
            return result(stdout=self.port_users.encode())
        if script.startswith("docker ps"):
            return result(stdout=f"{self.state}\n".encode() if self.state else b"")
        if script.startswith("docker run"):
            self.state = "running"
        if script.startswith("docker inspect"):
            return result(stdout=b'{"bridge": {}}')
        if "docker rm -fv" in script:
            self.state = ""
            return result(stdout=f"amnezia_volume_left={self.volume_left}\n".encode())
        return result()


@pytest.fixture
def upstream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "dummy"
    folder.mkdir()
    (folder / "Dockerfile").write_bytes(b"FROM alpine\r\n")
    (folder / "configure_container.sh").write_bytes(b"echo $DOCKERFILE_FOLDER\n")
    (folder / "start.sh").write_bytes(b"#!/bin/sh\necho $CONTAINER_NAME\n")
    (tmp_path / "remove_container.sh").write_bytes(
        (Path(__file__).parents[1] / "upstream" / "remove_container.sh").read_bytes()
    )
    monkeypatch.setattr("apps.protocols.services.container._UPSTREAM", tmp_path)
    return tmp_path


@pytest.fixture
def dummy() -> Iterator[type[DummyInstaller]]:
    installers.register(DummyInstaller)
    yield DummyInstaller
    installers.unregister(DUMMY)


def run_dummy(server: FakeDummyServer, port: int = 1080) -> tuple[str, Operation]:
    operation = OperationFactory()
    ctx = OperationContext(operation)
    return DummyInstaller(server.host(), ctx, port=port).run(), operation


def test_registry_knows_awg_and_refuses_a_second_installer_of_a_container() -> None:
    registry = InstallerRegistry()
    registry.register(AwgInstaller)

    assert installers.get("amnezia-awg2") is AwgInstaller
    assert list(registry) == [AwgInstaller]
    with pytest.raises(ValueError, match="already registered"):
        registry.register(AwgInstaller)
    with pytest.raises(KeyError):
        registry.get(DUMMY)


@pytest.mark.usefixtures("upstream")
def test_new_installer_runs_the_upstream_steps_in_order() -> None:
    server = FakeDummyServer()

    configured, _operation = run_dummy(server)

    assert configured == "configured"
    assert server.has_conntrack
    assert server.files["/opt/amnezia/amnezia-dummy/Dockerfile"] == b"FROM alpine\n"
    assert server.files["/opt/amnezia/start.sh"] == b"#!/bin/sh\necho amnezia-dummy\n"
    kinds = [script.split()[1] for script in server.scripts if script.startswith("docker ")]
    assert kinds[kinds.index("build") :] == [
        "build", "run", "inspect", "network", "exec", "exec",
    ]  # fmt: skip
    configure = server.scripts[-2]
    assert "echo /opt/amnezia/amnezia-dummy" in configure
    assert server.scripts[-1].startswith("docker exec -d amnezia-dummy")


@pytest.mark.usefixtures("upstream")
def test_busy_tcp_port_is_refused_before_the_build() -> None:
    server = FakeDummyServer(port_users="nginx 1 root 6u IPv4 TCP *:1080 (LISTEN)\n")

    with pytest.raises(SetupError) as caught:
        run_dummy(server)

    assert caught.value.code == SetupErrorCode.PORT_BUSY
    assert not any(script.startswith("docker build") for script in server.scripts)
    lsof = next(script for script in server.scripts if script.startswith("lsof "))
    assert "grep -i tcp | grep LISTEN" in lsof


@pytest.mark.usefixtures("upstream")
def test_existing_container_is_kept() -> None:
    server = FakeDummyServer(state="exited", has_conntrack=True)

    run_dummy(server)

    assert not any(script.startswith(("docker build", "docker run")) for script in server.scripts)
    assert "docker start amnezia-dummy" in server.scripts


@pytest.mark.usefixtures("upstream")
def test_removal_runs_the_upstream_script_and_the_volume_loop() -> None:
    server = FakeDummyServer(state="running")
    ctx = OperationContext(OperationFactory())

    ProtocolContainer(server.host(), ctx, DUMMY).remove_with_image(data_volume=True)

    script = next(script for script in server.scripts if "docker rm -fv" in script)
    assert "sudo" not in script
    assert "docker stop amnezia-dummy;" in script
    assert "docker rmi amnezia-dummy;" in script
    assert "docker volume rm -f amnezia-dummy-data" in script


@pytest.mark.usefixtures("upstream")
def test_volume_left_on_the_server_fails_the_removal() -> None:
    server = FakeDummyServer(state="running", volume_left="amnezia-dummy-data")
    ctx = OperationContext(OperationFactory())

    with pytest.raises(SetupError) as caught:
        ProtocolContainer(server.host(), ctx, DUMMY).remove_with_image(data_volume=True)

    assert caught.value.code == SetupErrorCode.DATA_VOLUME_LEFT


@pytest.fixture
def awg(monkeypatch: pytest.MonkeyPatch) -> FakeAwgServer:
    fake = FakeAwgServer()

    @contextmanager
    def connect(_server: Server) -> Iterator[RemoteHost]:
        yield fake.host()

    monkeypatch.setattr("apps.protocols.operations.connect", connect)
    return fake


def test_install_operation_puts_awg_into_the_panel(
    awg: FakeAwgServer, django_capture_on_commit_callbacks: OnCommit
) -> None:
    server = ServerFactory()
    admin = UserFactory(role=Role.ADMIN)
    request = InstallRequest(AwgInstaller, 40000, logs=True)

    with django_capture_on_commit_callbacks(execute=True):
        operation = start_install(server, request, admin)

    operation.refresh_from_db()
    assert operation.status == OperationStatus.SUCCEEDED
    assert awg.published_port == 40000
    assert awg.log_driver == "json-file"
    instance = ProtocolInstance.objects.get(server=server)
    assert instance.origin == InstanceOrigin.INSTALLED
    assert instance.port == 40000
    event = AuditEvent.objects.get(action=INSTALL_KIND)
    assert event.data == {"container": "amnezia-awg2", "port": 40000, "operation": operation.pk}


def test_install_page_starts_awg_on_a_free_low_port(admin_client: Client) -> None:
    # The legacy AmneziaWG holds the first low port, the probe the next one.
    server = ServerFactory(
        probe_port=5543, containers=[{"name": "amnezia-awg", "port": 5542, "transport": "udp"}]
    )
    url = f"/protocols/install/{server.pk}/"

    page = admin_client.get(url)
    response = admin_client.post(url, {"container": "amnezia-awg2", "port": ""})

    html = page.content.decode()
    assert "AmneziaWG 2.0" in html
    assert "5544/udp" in html
    assert "/wiki/Ports" in html
    operation = Operation.objects.get()
    assert operation.kind == INSTALL_KIND
    assert operation.params == {
        "server_id": server.pk, "container": "amnezia-awg2", "port": 5544, "logs": False,
        "server_host": server.host,
    }  # fmt: skip
    assert response.headers["Location"] == f"/operations/{operation.pk}/"


def test_install_page_keeps_the_app_port_of_other_protocols(admin_client: Client) -> None:
    server = ServerFactory()

    admin_client.post(f"/protocols/install/{server.pk}/", {"container": "amnezia-wireguard"})

    assert Operation.objects.get().params["port"] == 51820


def test_install_page_suggests_a_low_port_for_wireguard(admin_client: Client) -> None:
    server = ServerFactory(containers=[{"name": "amnezia-awg2", "port": 5542, "transport": "udp"}])

    page = admin_client.get(f"/protocols/install/{server.pk}/")

    assert page.context["form"].low_port_advice == [("amnezia-wireguard", 51820, 5543)]
    html = page.content.decode()
    assert "container === 'amnezia-wireguard' && !port" in html
    assert "for example 5543" in html


def test_install_page_refuses_a_port_taken_on_the_server(admin_client: Client) -> None:
    server = ServerFactory(
        probe_port=40000,
        containers=[{"name": "amnezia-xray", "port": 443, "transport": "tcp"}],
    )
    url = f"/protocols/install/{server.pk}/"

    probe = admin_client.post(url, {"container": "amnezia-awg2", "port": "40000"})
    other_transport = admin_client.post(url, {"container": "amnezia-awg2", "port": "443"})

    assert "port" in probe.context["form"].errors
    assert other_transport.status_code == 302


def test_installed_container_is_not_offered_again(admin_client: Client) -> None:
    server = ServerFactory(containers=[{"name": "amnezia-awg2", "port": 55424, "transport": "udp"}])
    url = f"/protocols/install/{server.pk}/"

    page = admin_client.get(url)
    response = admin_client.post(url, {"container": "amnezia-awg2"})

    assert "Already on the server." in page.content.decode()
    assert "container" in response.context["form"].errors
    assert not Operation.objects.exists()


@pytest.mark.usefixtures("dummy")
def test_registered_installer_shows_up_on_the_page(admin_client: Client) -> None:
    server = ServerFactory()

    page = admin_client.get(f"/protocols/install/{server.pk}/")

    assert "Dummy" in page.content.decode()
    assert "1080/tcp" in page.content.decode()


def test_install_page_is_for_admins_only(client: Client) -> None:
    server = ServerFactory()
    client.force_login(UserFactory(role=Role.MODERATOR))

    assert client.get(f"/protocols/install/{server.pk}/").status_code == 403
    assert (
        client.post(f"/protocols/install/{server.pk}/", {"container": "amnezia-awg2"}).status_code
        == 403
    )
