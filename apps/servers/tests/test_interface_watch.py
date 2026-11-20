from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import timedelta
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.notifications.events import Event
from apps.notifications.models import Notification
from apps.notifications.tests.factories import ChannelFactory
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.launcher import launch
from apps.protocols.models import InstanceOrigin, ProtocolInstance
from apps.protocols.services.lifecycle import STOP_KIND
from apps.protocols.tests.fake_awg import START_SCRIPT, FakeAwgServer
from apps.servers.models import InterfaceRepair, Server, ServerSample
from apps.servers.services.interface_watch import (
    INTERFACE_RULES,
    RESTART_KIND,
    InterfaceWatch,
)
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db

MINUTE = timedelta(minutes=1)
AWG2 = "amnezia-awg2"
OnCommit = Callable[..., AbstractContextManager[Any]]


@pytest.fixture(autouse=True)
def channel() -> None:
    ChannelFactory(
        events=[Event.INTERFACE_LOST, Event.INTERFACE_RESTORED, Event.INTERFACE_REPAIR_FAILED]
    )


class Readings:
    """A server read once a minute; each reading is checked as the monitor does."""

    def __init__(self, server: Server) -> None:
        self.server = server
        self.at = timezone.now() - timedelta(days=1)
        self._watch = InterfaceWatch(INTERFACE_RULES)

    def lost(self, count: int = 1) -> None:
        for _ in range(count):
            self.read({"name": AWG2, "state": "running", "interface_missing": True})

    def fine(self) -> None:
        self.read({"name": AWG2, "state": "running"})

    def stopped(self) -> None:
        self.read({"name": AWG2, "state": "exited"})

    def read(self, container: dict[str, object]) -> None:
        self.at += MINUTE
        ServerSample.objects.create(server=self.server, at=self.at, containers=[container])
        self._watch.check(self.server, self.at)

    def wait(self, period: timedelta) -> None:
        self.at += period


@pytest.fixture
def readings() -> Readings:
    return Readings(ServerFactory(name="Germany", host="198.51.100.7"))


def events() -> list[str]:
    return [n.event for n in Notification.objects.order_by("created_at", "pk")]


def restarts() -> list[Operation]:
    return list(Operation.objects.filter(kind=RESTART_KIND).order_by("pk"))


def finish_restarts() -> None:
    Operation.objects.filter(kind=RESTART_KIND).update(status=OperationStatus.FAILED)


def test_a_single_reading_without_the_interface_restarts_nothing(readings: Readings) -> None:
    readings.lost(2)
    readings.fine()
    readings.lost()

    assert restarts() == []
    assert events() == []


def test_a_lost_interface_is_told_and_the_container_restarted(readings: Readings) -> None:
    readings.lost(3)

    (restart,) = restarts()
    assert restart.params == {"server_id": readings.server.pk, "container": AWG2}
    (notice,) = Notification.objects.all()
    assert notice.event == Event.INTERFACE_LOST
    assert notice.data["container"] == AWG2
    assert notice.data["server"] == "Germany"
    repair = InterfaceRepair.objects.get()
    assert repair.attempts == 1


def test_the_watch_waits_while_the_restart_runs(readings: Readings) -> None:
    readings.lost(3)
    readings.wait(timedelta(hours=1))
    readings.lost(3)

    assert len(restarts()) == 1


def test_restarts_come_with_a_growing_pause_then_only_a_failure_is_told(
    readings: Readings,
) -> None:
    readings.lost(3)  # restart 1, the next one after 5 minutes
    finish_restarts()
    readings.lost(3)
    assert len(restarts()) == 1

    readings.wait(2 * MINUTE)
    readings.lost()  # restart 2, the next one after 10 minutes
    finish_restarts()
    readings.wait(5 * MINUTE)
    readings.lost()
    assert len(restarts()) == 2

    readings.wait(5 * MINUTE)
    readings.lost()  # restart 3, the last one
    finish_restarts()
    readings.wait(20 * MINUTE)
    readings.lost(3)

    assert len(restarts()) == 3
    assert events() == [Event.INTERFACE_LOST, Event.INTERFACE_REPAIR_FAILED]
    assert InterfaceRepair.objects.get().gave_up


def test_an_interface_back_is_told_once(readings: Readings) -> None:
    readings.lost(3)
    finish_restarts()

    readings.fine()
    readings.fine()

    assert events() == [Event.INTERFACE_LOST, Event.INTERFACE_RESTORED]
    assert not InterfaceRepair.objects.exists()


def test_a_container_stopped_by_the_administrator_is_neither_restarted_nor_told(
    readings: Readings,
) -> None:
    readings.stopped()
    readings.stopped()
    readings.stopped()
    assert restarts() == []

    readings.lost(3)
    finish_restarts()
    readings.stopped()

    assert events() == [Event.INTERFACE_LOST]
    assert not InterfaceRepair.objects.exists()


def test_nothing_is_restarted_while_an_operation_on_the_server_runs(readings: Readings) -> None:
    instance = ProtocolInstance.objects.create(
        server=readings.server,
        container=AWG2,
        port=55424,
        subnet="10.8.1.0/24",
        origin=InstanceOrigin.IMPORTED,
    )
    launch(STOP_KIND, {"instance_id": instance.pk})

    readings.lost(4)

    assert restarts() == []
    assert events() == []


def test_the_server_card_shows_the_lost_interface_and_the_restarts(
    readings: Readings, client: Client
) -> None:
    client.force_login(UserFactory(role=Role.ADMIN))
    readings.lost(3)
    readings.server.refresh_from_db()
    readings.server.record_sample(readings.server.samples.order_by("-at")[0])

    html = client.get(f"/servers/{readings.server.pk}/").content.decode()

    assert "No VPN interface, clients have no connection: amnezia-awg2" in html
    assert "The panel restarts amnezia-awg2 to bring its interface back" in html


@pytest.fixture
def awg(monkeypatch: pytest.MonkeyPatch) -> FakeAwgServer:
    host = FakeAwgServer(
        container_state="running",
        container_files={START_SCRIPT: b"#!/bin/bash\nawg-quick up /opt/amnezia/awg/awg0.conf\n"},
        awg_up=False,
    )

    @contextmanager
    def connect(_server: Server) -> Iterator[RemoteHost]:
        yield host.host()

    monkeypatch.setattr("apps.servers.operations.connect", connect)
    return host


def restart(on_commit: OnCommit) -> Operation:
    with on_commit(execute=True):
        operation = launch(RESTART_KIND, {"server_id": ServerFactory().pk, "container": AWG2})
    operation.refresh_from_db()
    return operation


def test_the_restart_brings_the_interface_back_under_the_server_lock(
    awg: FakeAwgServer, django_capture_on_commit_callbacks: OnCommit
) -> None:
    operation = restart(django_capture_on_commit_callbacks)

    assert operation.status == OperationStatus.SUCCEEDED
    assert awg.awg_up
    assert awg.locks == 1
    assert "restart" in awg.docker_calls


def test_the_restart_leaves_a_container_stopped_meanwhile(
    awg: FakeAwgServer, django_capture_on_commit_callbacks: OnCommit
) -> None:
    awg.container_state = "exited"

    operation = restart(django_capture_on_commit_callbacks)

    assert operation.status == OperationStatus.SUCCEEDED
    assert awg.container_state == "exited"
    assert "restart" not in awg.docker_calls


def test_the_restart_fails_when_the_interface_does_not_come_up(
    awg: FakeAwgServer, django_capture_on_commit_callbacks: OnCommit
) -> None:
    awg.awg_starts = False

    assert restart(django_capture_on_commit_callbacks).status == OperationStatus.FAILED
