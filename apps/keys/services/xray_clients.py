"""Adding, disabling, enabling, renaming and revoking clients of the XRay container."""

from collections.abc import Callable, Sequence
from contextlib import ExitStack

from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from amnezia_core.clients_table import ClientRecord, ClientsTable
from amnezia_core.errors import ProtectedClientError
from amnezia_core.xray import (
    XRAY_CLIENTS_TABLE_PATH,
    XRAY_CONFIG_PATH,
    XRAY_CONTAINER,
    XRAY_DIR,
    XRAY_OWN_CLIENT_PATH,
    XrayServerConfig,
)
from amnezia_core.xray_clients import XrayClientRegistry
from apps.keys.errors import PeerError, PeerErrorCode, explained_format_errors
from apps.keys.services.peers import StepRecorder
from apps.keys.services.swaps import Swap, SwapResult
from apps.keys.signals import xray_restarting
from apps.ssh.errors import RemoteFileNotFoundError
from apps.ssh.services import RemoteHost

_FILE_MODE = 0o600
_STAGED_PATH = f"{XRAY_DIR}/server.new.json"
_BACKUP_PATH = f"{XRAY_CONFIG_PATH}.bak"
_REJECTED_EXIT = 65

# Same order as upstream XrayConfigurator::uploadServerConfigAtomically: test the staged file,
# keep a backup and swap it in. The backup is removed only once xray serves the new config.
_SWAP_SCRIPT = (
    f"if ! xray -test -format json -config {_STAGED_PATH} > /tmp/xray_test.log 2>&1; then\n"
    f"  rm -f {_STAGED_PATH}\n"
    f"  exit {_REJECTED_EXIT}\n"
    "fi\n"
    f"if [ -f {XRAY_CONFIG_PATH} ]; then cp {XRAY_CONFIG_PATH} {_BACKUP_PATH}; fi\n"
    f"mv {_STAGED_PATH} {XRAY_CONFIG_PATH}\n"
)
# xrayProcessIsUp upstream: up to six seconds for xray to come up after the restart.
_SERVING_SCRIPT = (
    "for i in 1 2 3 4 5 6; do pgrep xray >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1"
)
_RESTORE_SCRIPT = f"cp {_BACKUP_PATH} {XRAY_CONFIG_PATH}"
_DROP_BACKUP_SCRIPT = f"rm -f {_BACKUP_PATH}"


class XrayClientManager:
    """Client changes by the safe write protocol (DESIGN.md 5.4), adapted to XRay.

    XRay has no live reload: like the Amnezia app, the panel restarts the container after
    changing server.json, so every XRay client of the server reconnects. A config that xray
    rejects never replaces the working one; if xray does not start, the old one is put back.
    Calls are idempotent: a backup left by an interrupted run makes the next run restart again.

    Every change also turns on traffic statistics, and puts them back after the app rebuilt
    server.json without them. Before a restart, which zeroes the counters, `xray_restarting`
    lets the traffic collector take a last reading of `source`.
    """

    def __init__(self, host: RemoteHost, steps: StepRecorder, source: str = "") -> None:
        self._host = host
        self._container = host.container(XRAY_CONTAINER)
        self._steps = steps
        self._source = source

    def add(self, client_id: str, name: str, *, user_id: int | None = None) -> None:
        """Add a client by its new VLESS UUID (uuid4), or put a disabled one back."""
        record = ClientRecord(name, timezone.localtime(), user_id)
        self._change(lambda registry: registry.add_client(client_id, record))

    def enable(self, client_id: str, name: str, *, user_id: int | None = None) -> None:
        self.add(client_id, name, user_id=user_id)

    def disable(self, client_id: str) -> None:
        """Drop the client from server.json; the panel keeps the UUID to enable it later."""
        self._change(lambda registry: registry.disable(client_id))

    def revoke(self, client_id: str) -> None:
        self._change(lambda registry: registry.revoke(client_id))

    def replace(self, swaps: Sequence[Swap]) -> SwapResult[None]:
        """Add the new clients and revoke the old ones with one write of server.json and so
        one restart. The app's own client cannot be revoked: its swap fails, nothing is added.
        """
        result = SwapResult[None]()

        def change(registry: XrayClientRegistry) -> None:
            for swap in swaps:
                try:
                    if swap.old:
                        registry.revoke(swap.old)
                except ProtectedClientError:
                    result.failed[swap.new] = PeerError(PeerErrorCode.PROTECTED_CLIENT).message
                    continue
                registry.add_client(swap.new, swap.record)
                result.done[swap.new] = None

        self._change(change)
        return result

    def rename(self, client_id: str, name: str) -> None:
        """Only clientsTable changes, so xray is not restarted unless statistics are off."""
        self._change(lambda registry: registry.rename(client_id, name))

    def update_config(self, change: Callable[[XrayServerConfig], None]) -> None:
        """Apply another change of server.json by the same protocol; no change, no restart."""
        self._change(lambda registry: change(registry.config))

    def _change(self, change: Callable[[XrayClientRegistry], None]) -> None:
        with ExitStack() as held:
            with self._steps.step(_("Lock the server")):
                held.enter_context(self._host.lock())
            with self._steps.step(_("Read the protocol configuration")):
                config_text = self._container.read_file(XRAY_CONFIG_PATH).decode()
                clients_text = self._read_optional(XRAY_CLIENTS_TABLE_PATH)
                unfinished = self._container.sh(f"[ -f {_BACKUP_PATH} ]").ok
                with explained_format_errors():
                    registry = XrayClientRegistry.parse(
                        config_text, clients_text, self._read_optional(XRAY_OWN_CLIENT_PATH).strip()
                    )
                    change(registry)
                    registry.config.enable_stats()
            config_changed = registry.config.serialize() != config_text
            if config_changed or unfinished:
                with self._steps.step(_("Update the server configuration")):
                    if config_changed:
                        self._swap_in(registry.config.serialize())
                    self._restart()
            if registry.clients.serialize() != ClientsTable.parse(clients_text).serialize():
                with self._steps.step(_("Update the client list")):
                    self._container.write_file_atomic(
                        XRAY_CLIENTS_TABLE_PATH, registry.clients.serialize().encode(), _FILE_MODE
                    )

    def _read_optional(self, path: str) -> str:
        try:
            return self._container.read_file(path).decode()
        except RemoteFileNotFoundError:
            return ""

    def _swap_in(self, text: str) -> None:
        self._container.write_file_atomic(_STAGED_PATH, text.encode(), _FILE_MODE)
        result = self._container.sh(_SWAP_SCRIPT)
        if result.exit_code == _REJECTED_EXIT:
            raise PeerError(PeerErrorCode.CONFIG_REJECTED)
        if not result.ok:
            raise PeerError(PeerErrorCode.SERVER_FILES_INVALID, f"swap exit {result.exit_code}")

    def _restart(self) -> None:
        self._steps.log(str(_("Restarting the container: all XRay clients reconnect.")))
        if self._source:
            xray_restarting.send_robust(type(self), source=self._source, host=self._host)
        self._host.docker("restart", XRAY_CONTAINER)
        if self._container.sh(_SERVING_SCRIPT).ok:
            self._container.run(_DROP_BACKUP_SCRIPT)
            return
        self._container.run(_RESTORE_SCRIPT)
        self._host.docker("restart", XRAY_CONTAINER)
        if not self._container.sh(_SERVING_SCRIPT).ok:
            raise PeerError(PeerErrorCode.NOT_SERVING, "down after rollback")
        self._container.run(_DROP_BACKUP_SCRIPT)
        raise PeerError(PeerErrorCode.NOT_SERVING, "rolled back")
