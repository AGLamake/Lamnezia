"""Integration tests against a real sshd container (ssh-integration job in ci.yml).

Locally: build and start the containers as the workflow does, set the LAMNEZIA_TEST_SSH_* variables
and run `uv run pytest -m integration apps/ssh`. Without the variables the tests are skipped.
"""

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from apps.ssh.services import RemoteHost, SshConnection, SshCredentials, SshTimeouts

HOST = os.environ.get("LAMNEZIA_TEST_SSH_HOST", "")
PORT = int(os.environ.get("LAMNEZIA_TEST_SSH_PORT", "22"))
KEY_FILE = os.environ.get("LAMNEZIA_TEST_SSH_KEY_FILE", "")
CONTAINER = os.environ.get("LAMNEZIA_TEST_SSH_CONTAINER", "")
TIMEOUTS = SshTimeouts(connect=10, command=10, lock=2)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    here = Path(__file__).parent
    for item in items:
        if here in item.path.parents:
            item.add_marker(pytest.mark.integration)
            item.add_marker(pytest.mark.django_db)
            if not HOST:
                item.add_marker(pytest.mark.skip(reason="LAMNEZIA_TEST_SSH_HOST is not set"))


def admin_credentials() -> SshCredentials:
    return SshCredentials(
        host=HOST, port=PORT, username="admin", private_key=Path(KEY_FILE).read_text()
    )


def plain_credentials(password: str = "plain-pass") -> SshCredentials:
    return SshCredentials(host=HOST, port=PORT, username="plain", password=password)


ConnectionFactory = Callable[[SshCredentials], SshConnection]


@pytest.fixture
def connect() -> Iterator[ConnectionFactory]:
    """Open connections that are closed after the test."""
    opened: list[SshConnection] = []

    def factory(credentials: SshCredentials) -> SshConnection:
        connection = SshConnection(credentials, timeouts=TIMEOUTS)
        connection.connect()
        opened.append(connection)
        return connection

    yield factory
    for connection in opened:
        connection.close()


@pytest.fixture
def host(connect: ConnectionFactory) -> RemoteHost:
    return RemoteHost(connect(admin_credentials()), TIMEOUTS)
