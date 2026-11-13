from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any

import pytest
from django.test import Client

from amnezia_core.awg import AWG_CLIENTS_TABLE_PATH, AWG_CONFIG_PATH, AWG_PSK_PATH
from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.keys.services.actions import KeyActions
from apps.keys.services.instances import Instance, instances
from apps.keys.tests.fake_server import FakeAwgServer
from apps.operations.models import Operation, OperationStatus

FIXTURES = Path(__file__).resolve().parents[3] / "amnezia_core" / "tests" / "fixtures"
SOURCE = "awg:1"
HOST = "203.0.113.10"
PSK = "u+xLktHw90auwUVtjZEJVpNCbfbFK8GNmGegY/Z0XYs="

OnCommit = Callable[..., AbstractContextManager[Any]]
Run = Callable[[Callable[[], Operation]], Operation]


@pytest.fixture
def admin_client(client: Client) -> Client:
    client.force_login(UserFactory(role=Role.ADMIN))
    return client


@pytest.fixture
def actions() -> KeyActions:
    return KeyActions(UserFactory(role=Role.ADMIN))


@pytest.fixture
def run(django_capture_on_commit_callbacks: OnCommit) -> Run:
    """Start an operation and run it at once, as the worker would."""

    def runner(start: Callable[[], Operation]) -> Operation:
        with django_capture_on_commit_callbacks(execute=True):
            operation = start()
        operation.refresh_from_db()
        assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
        return operation

    return runner


@pytest.fixture
def awg_server() -> Iterator[FakeAwgServer]:
    """An AmneziaWG 2.0 server registered as instance `awg:1` for the time of a test."""
    server = FakeAwgServer(
        {
            AWG_CONFIG_PATH: (FIXTURES / "awg0.conf").read_text(encoding="utf-8"),
            AWG_CLIENTS_TABLE_PATH: (FIXTURES / "clientsTable.json").read_text(encoding="utf-8"),
            AWG_PSK_PATH: f"{PSK}\n",
        }
    )

    def provider() -> list[Instance]:
        return [Instance(SOURCE, "Amsterdam", HOST, lambda: nullcontext(server.host()))]

    instances.register(provider)
    yield server
    instances.unregister(provider)
