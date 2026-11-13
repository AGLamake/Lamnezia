"""Fixtures of the AmneziaWG tests: the app's amnezia-awg2 server, its instance, a run.

Also the services a test changed, as `service_changed` names them.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from amnezia_core.awg import AWG_CLIENTS_TABLE_PATH, AWG_CONFIG_PATH, AWG_PSK_PATH
from amnezia_core.keys import KeyPair
from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.operations.models import Operation
from apps.protocols.models import InstanceOrigin, ProtocolInstance, ServiceInstance
from apps.protocols.signals import service_changed
from apps.protocols.tests.awg_case import APP_CONFIG, PSK, START, OnCommit, Start, fixture
from apps.protocols.tests.fake_awg import START_SCRIPT, FakeAwgServer
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import RemoteHost


@pytest.fixture
def awg() -> FakeAwgServer:
    return FakeAwgServer(
        container_state="running",
        container_networks={"bridge", "amnezia-dns-net"},
        container_files={
            AWG_CONFIG_PATH: fixture("awg0.conf"),
            AWG_CLIENTS_TABLE_PATH: fixture("clientsTable.json"),
            AWG_PSK_PATH: f"{PSK}\n".encode(),
            START_SCRIPT: START,
        },
        awg_up=True,
        published_port=55424,
    )


@pytest.fixture
def connected(awg: FakeAwgServer, monkeypatch: pytest.MonkeyPatch) -> FakeAwgServer:
    @contextmanager
    def connect(server: Server) -> Iterator[RemoteHost]:
        yield awg.host()

    monkeypatch.setattr("apps.protocols.operations.connect", connect)
    return awg


@pytest.fixture
def instance() -> ProtocolInstance:
    return ProtocolInstance.objects.create(
        server=ServerFactory(host="203.0.113.10", probe_port=3478),
        container="amnezia-awg2",
        port=55424,
        subnet="10.8.1.0/24",
        params=APP_CONFIG.awg_params,
        public_key=KeyPair.from_private_key(APP_CONFIG.private_key or "").public_key,
        private_key=APP_CONFIG.private_key or "",
        preshared_key=PSK,
        origin=InstanceOrigin.IMPORTED,
    )


@pytest.fixture
def changed_services() -> Iterator[list[ServiceInstance]]:
    """The services `service_changed` was sent for during the test."""
    changed: list[ServiceInstance] = []

    def receiver(sender: object, service: ServiceInstance, **kwargs: Any) -> None:
        changed.append(service)

    service_changed.connect(receiver, dispatch_uid="tests.changed_services")
    yield changed
    service_changed.disconnect(dispatch_uid="tests.changed_services")


@pytest.fixture
def admin() -> User:
    return UserFactory(role=Role.ADMIN)


@pytest.fixture
def start(django_capture_on_commit_callbacks: OnCommit) -> Start:
    """Start an operation and run it at once, as the worker would."""

    def runner(launch: Callable[[], Operation]) -> Operation:
        with django_capture_on_commit_callbacks(execute=True):
            operation = launch()
        operation.refresh_from_db()
        return operation

    return runner
