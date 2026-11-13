from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from amnezia_core.services import SFTP_CONTAINER
from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.operations.models import Operation, OperationStatus
from apps.protocols.models import InstanceOrigin, ProtocolInstance, ServiceInstance
from apps.protocols.services.lifecycle import (
    RESTART_SERVICE_KIND,
    SERVICE_LOGS_KIND,
    STOP_SERVICE_KIND,
    start_service_action,
)
from apps.protocols.tests.fake_awg import FakeAwgServer
from apps.servers.models import Server, ServerSample
from apps.servers.tests.factories import ServerFactory
from apps.ssh.errors import ErrorCode
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db

OnCommit = Callable[..., AbstractContextManager[Any]]


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> FakeAwgServer:
    """The docker side of the fake knows any container name, which is all a service needs."""
    server = FakeAwgServer(container_state="running")

    @contextmanager
    def connect(_server: Server) -> Iterator[RemoteHost]:
        yield server.host()

    monkeypatch.setattr("apps.protocols.operations.connect", connect)
    return server


@pytest.fixture
def service() -> ServiceInstance:
    server = ServerFactory(containers=[{"name": SFTP_CONTAINER, "port": 2222, "transport": "tcp"}])
    return ServiceInstance.objects.create(
        server=server, container=SFTP_CONTAINER, port=2222, origin=InstanceOrigin.IMPORTED
    )


@pytest.fixture
def run(
    django_capture_on_commit_callbacks: OnCommit, service: ServiceInstance
) -> Callable[[str], Operation]:
    admin: User = UserFactory(role=Role.ADMIN)

    def runner(kind: str) -> Operation:
        with django_capture_on_commit_callbacks(execute=True):
            operation = start_service_action(kind, service, admin)
        operation.refresh_from_db()
        return operation

    return runner


def step_log(operation: Operation) -> str:
    return "".join(step.log for step in operation.steps.all())


def test_restart_starts_a_stopped_service(
    host: FakeAwgServer, run: Callable[[str], Operation]
) -> None:
    host.container_state = "exited"

    operation = run(RESTART_SERVICE_KIND)

    assert operation.status == OperationStatus.SUCCEEDED
    assert host.container_state == "running"
    assert host.locks == 1
    assert AuditEvent.objects.filter(action=RESTART_SERVICE_KIND).exists()


def test_stop_leaves_the_service_on_the_server(
    host: FakeAwgServer, run: Callable[[str], Operation], service: ServiceInstance
) -> None:
    operation = run(STOP_SERVICE_KIND)

    assert operation.status == OperationStatus.SUCCEEDED
    assert host.container_state == "exited"
    assert ServiceInstance.objects.filter(pk=service.pk).exists()


def test_log_of_a_service_without_one_says_why(
    host: FakeAwgServer, run: Callable[[str], Operation]
) -> None:
    operation = run(SERVICE_LOGS_KIND)

    assert operation.status == OperationStatus.SUCCEEDED
    assert "logging turned off" in step_log(operation)
    assert "Install it again" in step_log(operation)
    assert not AuditEvent.objects.exists()


def test_log_of_a_service_shows_the_last_lines(
    host: FakeAwgServer, run: Callable[[str], Operation]
) -> None:
    host.container_log = b"Server listening on 0.0.0.0 port 22.\n"

    assert "Server listening" in step_log(run(SERVICE_LOGS_KIND))


@pytest.mark.parametrize("action", ["restart", "stop", "logs"])
def test_service_buttons_start_the_operation(
    admin_client: Client, service: ServiceInstance, action: str
) -> None:
    response = admin_client.post(f"/protocols/services/{service.pk}/{action}/")

    operation = Operation.objects.get()
    assert response.headers["Location"] == f"/operations/{operation.pk}/"


def test_unknown_service_action_is_not_found(
    admin_client: Client, service: ServiceInstance
) -> None:
    response = admin_client.post(f"/protocols/services/{service.pk}/reboot/")

    assert response.status_code == 404
    assert not Operation.objects.exists()


def monitored(server: Server, **states: str) -> None:
    sample = ServerSample.objects.create(
        server=server,
        at=timezone.now(),
        containers=[{"name": name, "state": state} for name, state in states.items()],
    )
    server.record_sample(sample)


def test_card_shows_the_monitored_state_and_starts_a_stopped_container(
    admin_client: Client, service: ServiceInstance
) -> None:
    server = service.server
    server.containers = [
        *server.containers,
        {"name": "amnezia-awg2", "port": 55424, "transport": "udp"},
    ]
    server.save(update_fields=["containers"])
    instance = ProtocolInstance.objects.create(
        server=server, container="amnezia-awg2", port=55424, origin=InstanceOrigin.IMPORTED
    )
    monitored(server, **{"amnezia-awg2": "exited", SFTP_CONTAINER: "running"})

    card = admin_client.get(f"/servers/{server.pk}/").content.decode()

    assert "exited" in card
    assert "running" in card
    assert f"/protocols/{instance.pk}/container/restart/" in card
    assert f"/protocols/{instance.pk}/container/stop/" not in card
    assert f"/protocols/services/{service.pk}/stop/" in card
    assert f"/protocols/services/{service.pk}/logs/" in card


def test_card_shows_no_state_when_the_server_did_not_answer(
    admin_client: Client, service: ServiceInstance
) -> None:
    sample = ServerSample.objects.create(
        server=service.server, at=timezone.now(), error=ErrorCode.CONNECTION_LOST
    )
    service.server.record_sample(sample)

    card = admin_client.get(f"/servers/{service.server.pk}/").content.decode()

    assert f"/protocols/services/{service.pk}/stop/" in card
    assert "badge-success" not in card
