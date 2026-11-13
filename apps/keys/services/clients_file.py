"""clientsTable of a certificate protocol (OpenVPN, IPsec), kept as the Amnezia app keeps it."""

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager

from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from amnezia_core.clients_table import CLIENT_NAME, ClientRecord, ClientsTable
from apps.keys.errors import explained_format_errors
from apps.keys.services.peers import StepRecorder
from apps.ssh.errors import RemoteFileNotFoundError
from apps.ssh.services import RemoteHost, ShellTarget

_FILE_MODE = 0o600

ClientsChange = Callable[[ClientsTable], object]


@contextmanager
def locked_server(host: RemoteHost, steps: StepRecorder) -> Iterator[None]:
    """Hold the server lock for a change of the clients, as a step of the operation."""
    with ExitStack() as held:
        with steps.step(_("Lock the server")):
            held.enter_context(host.lock())
        yield


class ClientsFile:
    """The clientsTable at `path` in a container; the caller holds the server lock.

    The file is written only if the change changed it, atomically.
    """

    def __init__(self, container: ShellTarget, path: str, steps: StepRecorder) -> None:
        self._container = container
        self._path = path
        self._steps = steps

    def register(self, client_id: str, name: str, user_id: int | None) -> None:
        record = ClientRecord(name, timezone.localtime(), user_id)
        self.update(lambda clients: clients.register(client_id, record))

    def rename(self, client_id: str, name: str) -> None:
        def rename(clients: ClientsTable) -> None:
            if client_id in clients:
                clients.update(client_id, {CLIENT_NAME: name})

        self.update(rename)

    def remove(self, client_id: str) -> None:
        self.update(lambda clients: clients.remove(client_id))

    def update(self, change: ClientsChange) -> None:
        text = self._read()
        with explained_format_errors():
            clients = ClientsTable.parse(text)
            change(clients)
        if clients.serialize() != ClientsTable.parse(text).serialize():
            with self._steps.step(_("Update the client list")):
                self._container.write_file_atomic(
                    self._path, clients.serialize().encode(), _FILE_MODE
                )

    def _read(self) -> str:
        try:
            return self._container.read_file(self._path).decode()
        except RemoteFileNotFoundError:
            return ""
