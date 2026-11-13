"""Issuing, disabling, enabling, renaming and revoking client certificates of OpenVPN."""

import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial

from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from amnezia_core.openvpn import (
    OPENVPN_CA_CERT_PATH,
    OPENVPN_CLIENTS_TABLE_PATH,
    OPENVPN_CONFIG_PATH,
    OPENVPN_CONTAINER,
    OPENVPN_DIR,
    OPENVPN_INDEX_PATH,
    OPENVPN_TA_KEY_PATH,
    CertificateIndex,
    ClientKey,
    OpenVpnSettings,
    certificate_path,
    certificate_serial,
    is_valid_client_id,
    request_path,
    upload_path,
)
from apps.keys.errors import PeerError, PeerErrorCode, explained_format_errors
from apps.keys.services.clients_file import ClientsFile, locked_server
from apps.keys.services.peers import StepRecorder
from apps.keys.services.swaps import Swap, SwapResult, apply_swaps
from apps.ssh.services import RemoteHost

_FILE_MODE = 0o600
_REQUEST_MODE = 0o644
# Left while the CRL on the server is older than pki/index.txt or openvpn has not reloaded it,
# so a run interrupted after changing the index finishes the job when it is repeated.
_PENDING_PATH = f"{OPENVPN_DIR}/crl.pending"
_EASYRSA = f"export EASYRSA_BATCH=1; cd {OPENVPN_DIR} && easyrsa"
# As the Amnezia app does after a revoke: openvpn drops privileges, so the CRL is world-readable.
_CRL_SCRIPT = f"{_EASYRSA} gen-crl && chmod 666 pki/crl.pem && cp pki/crl.pem crl.pem"
# The CRL openvpn ran with, put back if openvpn does not start with the new one.
_CRL_PATH = f"{OPENVPN_DIR}/crl.pem"
_PREVIOUS_CRL_PATH = f"{OPENVPN_DIR}/crl.pem.previous"
_SAVE_CRL_SCRIPT = (
    f"if [ -f {_CRL_PATH} ]; then cp -p {_CRL_PATH} {_PREVIOUS_CRL_PATH}; "
    f"else rm -f {_PREVIOUS_CRL_PATH}; fi"
)
_RESTORE_CRL_SCRIPT = f"[ -f {_PREVIOUS_CRL_PATH} ] && cp -p {_PREVIOUS_CRL_PATH} {_CRL_PATH}"
_STOP_SCRIPT = "killall -KILL openvpn 2>/dev/null; true"
# start.sh starts openvpn from / so that ipp.txt and the status log stay where they were.
_START_COMMAND = f"cd / && openvpn --config {OPENVPN_CONFIG_PATH} --daemon"
_SERVING_SCRIPT = (
    "for i in 1 2 3 4 5 6; do pgrep openvpn >/dev/null 2>&1 && exit 0; sleep 1; done; exit 1"
)

IndexChange = Callable[[CertificateIndex, int], None]


@dataclass(frozen=True, repr=False)
class ClientFiles:
    """What a client profile needs from the server; certificates are public, ta.key is not."""

    settings: OpenVpnSettings
    ca_cert: str
    client_cert: str
    ta_key: str

    def __repr__(self) -> str:
        return f"ClientFiles(settings={self.settings!r})"


class OpenVpnClientManager:
    """Client certificates by the easy-rsa CA of the container, as the Amnezia app does.

    A disabled client keeps its certificate: it is only marked revoked in pki/index.txt, and
    enabling takes the mark off. After every change of the CRL openvpn is restarted, so the
    clients it now refuses are cut off at once; the others reconnect by themselves. Calls are
    idempotent and run under the server lock.
    """

    def __init__(self, host: RemoteHost, steps: StepRecorder) -> None:
        self._host = host
        self._container = host.container(OPENVPN_CONTAINER)
        self._steps = steps
        self._clients = ClientsFile(self._container, OPENVPN_CLIENTS_TABLE_PATH, steps)

    def issue(
        self, client_id: str, key: ClientKey, name: str, *, user_id: int | None = None
    ) -> ClientFiles:
        """Sign a certificate for the client's own key and name it in clientsTable."""
        self._check_id(client_id)
        with locked_server(self._host, self._steps):
            with self._steps.step(_("Sign the client certificate")):
                self._sign(client_id, key)
            self._clients.register(client_id, name, user_id)
            with self._steps.step(_("Read the protocol configuration")):
                return self._client_files(client_id)

    def disable(self, client_id: str) -> None:
        """Revoke the certificate in pki/index.txt; it stays issued to be enabled later."""
        self._check_id(client_id)
        with locked_server(self._host, self._steps):
            if self._has_certificate(client_id):
                self._edit_index(
                    client_id, lambda index, serial: index.hold(serial, timezone.now())
                )
            else:
                self._steps.log(str(_("The certificate is already revoked.")))
            self._apply_crl()

    def enable(self, client_id: str, name: str, *, user_id: int | None = None) -> None:
        self._check_id(client_id)
        with locked_server(self._host, self._steps):
            if not self._has_certificate(client_id):
                raise PeerError(PeerErrorCode.CERTIFICATE_GONE, client_id)
            self._edit_index(client_id, lambda index, serial: index.release(serial))
            self._apply_crl()
            self._clients.register(client_id, name, user_id)

    def revoke(self, client_id: str) -> None:
        """`easyrsa revoke`, as in the Amnezia app; the client leaves clientsTable."""
        self._check_id(client_id)
        with locked_server(self._host, self._steps):
            self._revoke_certificate(client_id)
            self._apply_crl()
            self._clients.remove(client_id)

    def replace(
        self, swaps: Sequence[Swap], keys: Mapping[str, ClientKey]
    ) -> SwapResult[ClientFiles]:
        """Sign the new certificates (`keys` by new client) and revoke the old ones under one
        lock; the CRL is applied, openvpn restarted and clientsTable written once.

        The certificates change in one step with a log line per client. A swap whose
        certificate fails is left out and the others go on.
        """
        for swap in swaps:
            self._check_id(swap.new)
            if swap.old:
                self._check_id(swap.old)
        result = SwapResult[ClientFiles]()
        with locked_server(self._host, self._steps):
            with self._steps.step(_("Replace the client certificates")):
                done = [swap for swap in swaps if self._swap(swap, keys[swap.new], result)]
            self._apply_crl()
            self._clients.update(partial(apply_swaps, done))
            with self._steps.step(_("Read the protocol configuration")):
                for swap in done:
                    result.done[swap.new] = self._client_files(swap.new)
        return result

    def rename(self, client_id: str, name: str) -> None:
        """Only clientsTable changes, so openvpn is not restarted."""

        with locked_server(self._host, self._steps):
            self._clients.rename(client_id, name)

    def assign(self, client_id: str, name: str, *, user_id: int | None) -> None:
        with locked_server(self._host, self._steps):
            self._clients.register(client_id, name, user_id)

    def _swap(self, swap: Swap, key: ClientKey, result: SwapResult[ClientFiles]) -> bool:
        try:
            self._sign(swap.new, key)
            if swap.old and self._has_certificate(swap.old):
                self._change_index(swap.old, lambda index, serial: index.release(serial))
                self._easyrsa_revoke(swap.old)
        except PeerError as error:
            result.failed[swap.new] = error.message
            self._steps.log(swap.failed_line(error.message))
            return False
        self._steps.log(swap.done_line())
        return True

    def _revoke_certificate(self, client_id: str) -> None:
        if self._has_certificate(client_id):
            # A disabled certificate is put back first: easy-rsa refuses to revoke it twice.
            self._edit_index(client_id, lambda index, serial: index.release(serial))
            with self._steps.step(_("Revoke the client certificate")):
                self._easyrsa_revoke(client_id)

    def _easyrsa_revoke(self, client_id: str) -> None:
        self._mark_pending()
        self._easyrsa(f"revoke {client_id}")

    @staticmethod
    def _check_id(client_id: str) -> None:
        if not is_valid_client_id(client_id):
            raise PeerError(PeerErrorCode.SERVER_FILES_INVALID, f"bad client id {client_id!r}")

    def _sign(self, client_id: str, key: ClientKey) -> None:
        """Each step is skipped when an earlier run already did it."""
        if self._has_certificate(client_id):
            return
        if not self._has_file(request_path(client_id)):
            with explained_format_errors():
                request = key.certificate_request(client_id)
            self._container.write_file_atomic(
                upload_path(client_id), request.encode(), _REQUEST_MODE
            )
            self._easyrsa(f"import-req {shlex.quote(upload_path(client_id))} {client_id}")
        self._easyrsa(f"sign-req client {client_id}")

    def _edit_index(self, client_id: str, change: IndexChange) -> None:
        with self._steps.step(_("Update the certificate list")):
            self._change_index(client_id, change)

    def _change_index(self, client_id: str, change: IndexChange) -> None:
        text = self._container.read_file(OPENVPN_INDEX_PATH).decode()
        issued = self._container.read_file(certificate_path(client_id)).decode()
        with explained_format_errors():
            index = CertificateIndex.parse(text)
            change(index, certificate_serial(issued))
        if index.serialize() != text:
            self._mark_pending()
            self._container.write_file_atomic(
                OPENVPN_INDEX_PATH, index.serialize().encode(), _FILE_MODE
            )

    def _apply_crl(self) -> None:
        """Make the CRL from pki/index.txt and restart openvpn, if a change is pending.

        If openvpn does not start with the new CRL, the previous one comes back and openvpn
        serves with it; the change stays pending for the next run.
        """
        if not self._has_file(_PENDING_PATH):
            return
        with self._steps.step(_("Apply the revocation list")):
            self._container.run(_SAVE_CRL_SCRIPT)
            if not self._container.sh(_CRL_SCRIPT).ok:
                raise PeerError(PeerErrorCode.CERTIFICATE_REJECTED, "gen-crl failed")
            try:
                self._restart()
            except PeerError:
                self._bring_back_crl()
                raise
            self._container.run(f"rm -f {_PENDING_PATH} {_PREVIOUS_CRL_PATH}")

    def _bring_back_crl(self) -> None:
        """The previous CRL and openvpn with it; a failure here leaves the first error."""
        with self._steps.step(_("Bring back the previous revocation list")):
            if not self._container.sh(_RESTORE_CRL_SCRIPT).ok:
                self._steps.log(str(_("There is no previous revocation list.")))
                return
            try:
                self._restart()
            except PeerError:
                self._steps.log(str(_("OpenVPN did not start with the previous list either.")))
                return
            self._steps.log(str(_("OpenVPN serves with the previous revocation list.")))

    def _restart(self) -> None:
        self._steps.log(str(_("Restarting openvpn: all OpenVPN clients reconnect.")))
        self._container.run(_STOP_SCRIPT)
        self._host.docker("exec", "-d", OPENVPN_CONTAINER, "sh", "-c", _START_COMMAND)
        if not self._container.sh(_SERVING_SCRIPT).ok:
            raise PeerError(PeerErrorCode.OPENVPN_DOWN)

    def _client_files(self, client_id: str) -> ClientFiles:
        config = self._container.read_file(OPENVPN_CONFIG_PATH).decode()
        with explained_format_errors():
            settings = OpenVpnSettings.from_server_config(config)
        return ClientFiles(
            settings,
            self._container.read_file(OPENVPN_CA_CERT_PATH).decode(),
            self._container.read_file(certificate_path(client_id)).decode(),
            self._container.read_file(OPENVPN_TA_KEY_PATH).decode(),
        )

    def _easyrsa(self, arguments: str) -> None:
        result = self._container.sh(f"{_EASYRSA} {arguments}")
        if not result.ok:
            raise PeerError(PeerErrorCode.CERTIFICATE_REJECTED, f"easyrsa exit {result.exit_code}")

    def _mark_pending(self) -> None:
        self._container.run(f"touch {_PENDING_PATH}")

    def _has_certificate(self, client_id: str) -> bool:
        return self._has_file(certificate_path(client_id))

    def _has_file(self, path: str) -> bool:
        return self._container.sh(f"[ -f {shlex.quote(path)} ]").ok
