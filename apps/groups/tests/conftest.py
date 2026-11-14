"""Fixtures of the group tests: a leading AmneziaWG 3.1 server and a standalone one, both fake.

`servers` connects the operations to the fake of each server by its address.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest

from amnezia_core.awg import (
    AWG_CLIENTS_TABLE_PATH,
    AWG_CONFIG_PATH,
    AWG_PSK_PATH,
    AwgServerConfig,
)
from amnezia_core.awg_backup import AwgServerFiles
from amnezia_core.awg_group import standalone_files
from amnezia_core.awg_install import upgraded_awg_params
from amnezia_core.keys import KeyPair
from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.groups.models import ServerGroup
from apps.groups.services.membership import create_group, start_join
from apps.operations.models import Operation, OperationStatus
from apps.protocols.models import InstanceOrigin, ProtocolInstance
from apps.protocols.tests.awg_case import APP_CONFIG, PSK, START, OnCommit, Start, fixture
from apps.protocols.tests.fake_awg import START_SCRIPT, FakeAwgServer
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.errors import ErrorCode, SshError
from apps.ssh.services import RemoteHost

Run = Callable[[], Operation]
Fakes = dict[str, FakeAwgServer]

LEADER_HOST = "203.0.113.10"
JOINER_HOST = "203.0.113.20"
PORT = 55424


def _leader_files() -> AwgServerFiles:
    config = AwgServerConfig.parse(fixture("awg0.conf").decode())
    config.update_interface(PORT, upgraded_awg_params(APP_CONFIG.awg_params))
    return AwgServerFiles(config.serialize(), fixture("clientsTable.json").decode(), PSK)


LEADER_FILES = _leader_files()


def fake_server(files: AwgServerFiles) -> FakeAwgServer:
    return FakeAwgServer(
        container_state="running",
        container_networks={"bridge", "amnezia-dns-net"},
        container_files={
            AWG_CONFIG_PATH: files.config.encode(),
            AWG_CLIENTS_TABLE_PATH: files.clients_table.encode(),
            AWG_PSK_PATH: f"{files.preshared_key}\n".encode(),
            START_SCRIPT: START,
        },
        awg_up=True,
        published_port=PORT,
    )


def instance_of(host: str, files: AwgServerFiles) -> ProtocolInstance:
    """The panel's record of a server running `files`."""
    config = AwgServerConfig.parse(files.config)
    private_key = config.private_key or ""
    return ProtocolInstance.objects.create(
        server=ServerFactory(host=host),
        container="amnezia-awg2",
        port=PORT,
        subnet=str(config.address.network),
        params=config.awg_params,
        public_key=KeyPair.from_private_key(private_key).public_key,
        private_key=private_key,
        preshared_key=files.preshared_key,
        origin=InstanceOrigin.IMPORTED,
    )


@pytest.fixture
def leader_awg() -> FakeAwgServer:
    return fake_server(LEADER_FILES)


@pytest.fixture
def joiner_awg() -> FakeAwgServer:
    """A standalone server of its own with the clients of an Amnezia app."""
    fresh = standalone_files(APP_CONFIG.address, PORT)
    return fake_server(
        AwgServerFiles(fresh.config, fixture("clientsTable.json").decode(), fresh.preshared_key)
    )


@pytest.fixture
def down() -> set[str]:
    """Addresses of the fake servers that do not answer now."""
    return set()


@pytest.fixture
def servers(
    leader_awg: FakeAwgServer,
    joiner_awg: FakeAwgServer,
    down: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, FakeAwgServer]:
    fakes = {LEADER_HOST: leader_awg, JOINER_HOST: joiner_awg}

    @contextmanager
    def connect(server: Server) -> Iterator[RemoteHost]:
        if server.host in down:
            raise SshError(ErrorCode.HOST_UNREACHABLE, server.host)
        yield fakes[server.host].host()

    for module in (
        "groups.services.cloning",
        "groups.services.replication",
        "protocols.providers",
        "backups.services.snapshot",
    ):
        monkeypatch.setattr(f"apps.{module}.connect", connect)
    return fakes


@pytest.fixture
def leader() -> ProtocolInstance:
    return instance_of(LEADER_HOST, LEADER_FILES)


@pytest.fixture
def joiner(joiner_awg: FakeAwgServer) -> ProtocolInstance:
    return instance_of(JOINER_HOST, server_files(joiner_awg))


@pytest.fixture
def admin() -> User:
    return UserFactory(role=Role.ADMIN)


@pytest.fixture
def group(leader: ProtocolInstance, admin: User) -> ServerGroup:
    return create_group(ServerGroup(name="Europe"), leader, actor=admin)


@pytest.fixture
def start(django_capture_on_commit_callbacks: OnCommit) -> Start:
    """Start an operation and run it at once, as the worker would."""

    def runner(launch: Callable[[], Operation]) -> Operation:
        with django_capture_on_commit_callbacks(execute=True):
            operation = launch()
        operation.refresh_from_db()
        return operation

    return runner


@pytest.fixture
def join(
    servers: Fakes, group: ServerGroup, joiner: ProtocolInstance, admin: User, start: Start
) -> Run:
    """Run the joining of the standalone server into the group."""
    return lambda: start(lambda: start_join(group, joiner, user=admin))


def succeeded(operation: Operation) -> None:
    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message


def server_files(awg: FakeAwgServer) -> AwgServerFiles:
    """The files the fake server runs with now."""
    files = awg.container_files
    return AwgServerFiles(
        files[AWG_CONFIG_PATH].decode(),
        files[AWG_CLIENTS_TABLE_PATH].decode(),
        files[AWG_PSK_PATH].decode().strip(),
    )
