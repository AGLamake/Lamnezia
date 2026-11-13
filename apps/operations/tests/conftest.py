from collections.abc import Callable, Iterator

import pytest

from apps.operations.services.registry import Handler, operations
from apps.ssh.errors import ErrorCode, SshCommandError

Register = Callable[[str, Handler], None]


@pytest.fixture
def register_kind() -> Iterator[Register]:
    """Register operation kinds for one test in the shared registry the Celery task uses."""
    names: list[str] = []

    def register(name: str, handler: Handler) -> None:
        operations.register(name, f"Title of {name}")(handler)
        names.append(name)

    yield register
    for name in names:
        operations.unregister(name)


class FakeServer:
    """A VPN server whose config reload fails the first `sync_failures` times."""

    def __init__(self, sync_failures: int = 0) -> None:
        self.peers: list[str] = []
        self.syncs = 0
        self._sync_failures = sync_failures

    def add_peer(self, name: str) -> None:
        if name not in self.peers:
            self.peers.append(name)

    def sync(self) -> None:
        if self._sync_failures:
            self._sync_failures -= 1
            raise SshCommandError(ErrorCode.COMMAND_FAILED, "awg syncconf: Device busy")
        self.syncs += 1
