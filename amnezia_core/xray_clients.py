"""Client changes of the XRay container: server.json and clientsTable edited together."""

from amnezia_core.clients_table import CLIENT_NAME, ClientRecord, ClientsTable
from amnezia_core.errors import ProtectedClientError
from amnezia_core.xray import XrayServerConfig


class XrayClientRegistry:
    """server.json and clientsTable of the XRay container as read from the server.

    Every change is idempotent, so a failed operation can be run again. The client of the app
    that installed the server (xray_uuid.key) is never removed: the owner would lose access.
    """

    def __init__(
        self, config: XrayServerConfig, clients: ClientsTable, own_client_id: str | None
    ) -> None:
        self.config = config
        self.clients = clients
        self._own_client_id = own_client_id

    @classmethod
    def parse(
        cls, config_text: str, clients_text: str, own_client_id: str | None
    ) -> "XrayClientRegistry":
        return cls(
            XrayServerConfig.parse(config_text), ClientsTable.parse(clients_text), own_client_id
        )

    def add_client(self, client_id: str, record: ClientRecord) -> None:
        """Add a new client or put a disabled one back; its UUID is the client's credential."""
        if client_id not in self.config.client_ids:
            self.config.add_client(client_id, self.config.settings().server_flow)
        self.clients.register(client_id, record)

    def rename(self, client_id: str, name: str) -> None:
        """Set the name the app shows; a client missing from clientsTable is skipped."""
        if client_id in self.clients:
            self.clients.update(client_id, {CLIENT_NAME: name})

    def disable(self, client_id: str) -> None:
        """Take the client out of server.json; the clientsTable record keeps its name."""
        self._check_not_own(client_id)
        self.config.remove_client(client_id)

    def revoke(self, client_id: str) -> None:
        self._check_not_own(client_id)
        self.config.remove_client(client_id)
        self.clients.remove(client_id)

    def _check_not_own(self, client_id: str) -> None:
        if client_id == self._own_client_id:
            raise ProtectedClientError("This client belongs to the app that installed the server.")
