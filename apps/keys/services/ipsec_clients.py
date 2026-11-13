"""Issuing, disabling, enabling, renaming and revoking client certificates of IPsec (IKEv2)."""

import base64
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial

from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from amnezia_core.ipsec import (
    CA_NICKNAME,
    IKEV2_CONFIG_PATH,
    IPSEC_CA_CERT_PATH,
    IPSEC_CLIENTS_DIR,
    IPSEC_CLIENTS_TABLE_PATH,
    IPSEC_CONTAINER,
    NSS_DATABASE,
    client_states,
    crl_entry,
    crl_removal,
    export_path,
    is_valid_client_id,
    listed_clients,
    pem_serial,
    revoked_serials,
    server_identity,
)
from apps.keys.errors import PeerError, PeerErrorCode, explained_format_errors
from apps.keys.services.clients_file import ClientsFile, locked_server
from apps.keys.services.peers import StepRecorder
from apps.keys.services.swaps import Swap, SwapResult, apply_swaps
from apps.ssh.services import RemoteHost, ShellTarget

_NSS = ("-d", NSS_DATABASE)
_CA_CRL = shlex.join([*_NSS, "-n", CA_NICKNAME])
_CRL_LIST = f"crlutil -L {_CA_CRL} 2>/dev/null"
_CRL_GENERATE = f"crlutil -G {_CA_CRL} -c /dev/null >/dev/null"
# crlutil reads the lines of the change from stdin.
_CRL_MODIFY = f"crlutil -M {_CA_CRL} >/dev/null"
# Libreswan reads the CRLs of the database again.
_RELOAD_CRLS = "ipsec crls >/dev/null 2>&1; true"


@dataclass(frozen=True, repr=False)
class IssuedIpsecClient:
    """What a client profile needs: the PKCS#12 with the client's key, the CA, the server id."""

    p12: str
    ca_cert: str
    server_ip: str

    def __repr__(self) -> str:
        return f"IssuedIpsecClient(server_ip={self.server_ip!r})"


class IpsecDatabase:
    """Read-only view of the NSS database of the container: client certificates and the CRL."""

    def __init__(self, container: ShellTarget) -> None:
        self._container = container

    def client_ids(self) -> list[str]:
        """Client certificates the panel can manage; the CA and the server one are left out."""
        listing = self._container.sh(shlex.join(["certutil", "-L", *_NSS]))
        if not listing.ok:
            raise PeerError(PeerErrorCode.IPSEC_REJECTED, f"exit {listing.exit_code}")
        return listed_clients(listing.output)

    def serial(self, client_id: str) -> int | None:
        """Serial of the client certificate; None if the database has no such certificate."""
        listing = self._container.sh(shlex.join(["certutil", "-L", *_NSS, "-n", client_id, "-a"]))
        if not listing.ok:
            return None
        with explained_format_errors():
            return pem_serial(listing.output)

    def has_crl(self) -> bool:
        return self._container.sh(_CRL_LIST).ok

    def revoked(self) -> set[int]:
        listing = self._container.sh(_CRL_LIST)
        return revoked_serials(listing.output) if listing.ok else set()

    def clients_in_force(self) -> list[str]:
        """Client certificates the CRL does not name."""
        revoked = self.revoked()
        clients = self.client_ids()
        if not revoked:
            return clients
        return [client for client in clients if self.serial(client) not in revoked]


class IpsecClientManager:
    """Client certificates by the CA in the NSS database of the container, as the app makes them.

    pluto accepts every certificate the CA signed, so a disabled client is put into the CRL of
    the CA and its sessions are deleted; enabling takes it out of the CRL. A revoked client
    stays in the CRL and leaves the database. Calls are idempotent and run under the server lock.
    """

    def __init__(self, host: RemoteHost, steps: StepRecorder) -> None:
        self._host = host
        self._container = host.container(IPSEC_CONTAINER)
        self._steps = steps
        self._database = IpsecDatabase(self._container)
        self._clients = ClientsFile(self._container, IPSEC_CLIENTS_TABLE_PATH, steps)

    def issue(self, client_id: str, name: str, *, user_id: int | None = None) -> IssuedIpsecClient:
        """Make the client certificate with its key in the container and export it."""
        self._check_id(client_id)
        with locked_server(self._host, self._steps):
            with self._steps.step(_("Sign the client certificate")):
                if self._database.serial(client_id) is None:
                    self._nss(_sign_script(client_id))
                p12 = self._export(client_id)
            self._clients.register(client_id, name, user_id)
            with self._steps.step(_("Read the protocol configuration")):
                return IssuedIpsecClient(p12, self._read(IPSEC_CA_CERT_PATH), self._server_ip())

    def disable(self, client_id: str) -> None:
        """Put the certificate into the CRL and end the client's sessions."""
        self._check_id(client_id)
        with locked_server(self._host, self._steps):
            serial = self._database.serial(client_id)
            if serial is None:
                self._steps.log(str(_("The certificate is already gone.")))
                return
            self._block(client_id, serial)

    def enable(self, client_id: str, name: str, *, user_id: int | None = None) -> None:
        self._check_id(client_id)
        with locked_server(self._host, self._steps):
            serial = self._database.serial(client_id)
            if serial is None:
                raise PeerError(PeerErrorCode.CERTIFICATE_GONE, client_id)
            if serial in self._database.revoked():
                with self._steps.step(_("Apply the revocation list")):
                    self._nss(_CRL_MODIFY, stdin=crl_removal(serial))
                    self._container.sh(_RELOAD_CRLS)
            self._clients.register(client_id, name, user_id)

    def revoke(self, client_id: str) -> None:
        """Block the certificate for good and delete it with its key, as hwdsl2 does."""
        self._check_id(client_id)
        with locked_server(self._host, self._steps):
            self._revoke_certificate(client_id)
            self._clients.remove(client_id)

    def replace(self, swaps: Sequence[Swap]) -> SwapResult[IssuedIpsecClient]:
        """Make the new certificates and revoke the old ones under one lock; clientsTable is
        written once. The certificates change in one step with a log line per client. A swap
        whose certificate fails is left out and the others go on.
        """
        for swap in swaps:
            self._check_id(swap.new)
            if swap.old:
                self._check_id(swap.old)
        result = SwapResult[IssuedIpsecClient]()
        with locked_server(self._host, self._steps):
            with self._steps.step(_("Replace the client certificates")):
                exports = {swap.new: p12 for swap in swaps if (p12 := self._swap(swap, result))}
            done = [swap for swap in swaps if swap.new in exports]
            self._clients.update(partial(apply_swaps, done))
            with self._steps.step(_("Read the protocol configuration")):
                ca_cert, server_ip = self._read(IPSEC_CA_CERT_PATH), self._server_ip()
                for client_id, p12 in exports.items():
                    result.done[client_id] = IssuedIpsecClient(p12, ca_cert, server_ip)
        return result

    def rename(self, client_id: str, name: str) -> None:
        """Only clientsTable changes."""
        with locked_server(self._host, self._steps):
            self._clients.rename(client_id, name)

    def assign(self, client_id: str, name: str, *, user_id: int | None) -> None:
        with locked_server(self._host, self._steps):
            self._clients.register(client_id, name, user_id)

    def _swap(self, swap: Swap, result: SwapResult[IssuedIpsecClient]) -> str:
        """The PKCS#12 of the new client, or "" when its swap failed."""
        try:
            if self._database.serial(swap.new) is None:
                self._nss(_sign_script(swap.new))
            p12 = self._export(swap.new)
            if swap.old:
                self._drop_certificate(swap.old)
        except PeerError as error:
            result.failed[swap.new] = error.message
            self._steps.log(swap.failed_line(error.message))
            return ""
        self._steps.log(swap.done_line())
        return p12

    def _revoke_certificate(self, client_id: str) -> None:
        serial = self._database.serial(client_id)
        if serial is not None:
            self._block(client_id, serial)
            with self._steps.step(_("Revoke the client certificate")):
                self._delete_certificate(client_id)
        self._remove_export(client_id)

    def _drop_certificate(self, client_id: str) -> None:
        """`_revoke_certificate` within the step of a batch."""
        serial = self._database.serial(client_id)
        if serial is not None:
            self._add_to_crl(serial)
            self._end_sessions(client_id)
            self._delete_certificate(client_id)
        self._remove_export(client_id)

    def _delete_certificate(self, client_id: str) -> None:
        self._nss(shlex.join(["certutil", "-F", *_NSS, "-n", client_id]))

    def _remove_export(self, client_id: str) -> None:
        self._container.run(f"rm -f {shlex.quote(export_path(client_id))}")

    @staticmethod
    def _check_id(client_id: str) -> None:
        if not is_valid_client_id(client_id):
            raise PeerError(PeerErrorCode.SERVER_FILES_INVALID, f"bad client id {client_id!r}")

    def _block(self, client_id: str, serial: int) -> None:
        with self._steps.step(_("Apply the revocation list")):
            self._add_to_crl(serial)
        with self._steps.step(_("End the sessions of the client")):
            self._end_sessions(client_id)

    def _add_to_crl(self, serial: int) -> None:
        if serial not in self._database.revoked():
            if not self._database.has_crl():
                self._nss(_CRL_GENERATE)
            self._nss(_CRL_MODIFY, stdin=crl_entry(serial, timezone.now()))
            self._container.sh(_RELOAD_CRLS)

    def _end_sessions(self, client_id: str) -> None:
        """Best effort: a session pluto already dropped is no failure."""
        traffic = self._container.sh("ipsec whack --trafficstatus").output
        states = self._container.sh("ipsec whack --showstates").output
        for state in client_states(traffic, states, client_id):
            self._container.sh(f"ipsec whack --deletestate {state}")

    def _export(self, client_id: str) -> str:
        """Base64 of the PKCS#12 pk12util exports with an empty password, as the app does."""
        path = export_path(client_id)
        export = shlex.join(["pk12util", "-W", "", *_NSS, "-n", client_id, "-o", path])
        try:
            self._nss(
                f"mkdir -p {IPSEC_CLIENTS_DIR}; rm -f {shlex.quote(path)}; {export} >/dev/null"
            )
            return base64.b64encode(self._container.read_file(path)).decode()
        finally:
            self._container.sh(f"rm -f {shlex.quote(path)}")

    def _server_ip(self) -> str:
        with explained_format_errors():
            return server_identity(self._read(IKEV2_CONFIG_PATH))

    def _read(self, path: str) -> str:
        return self._container.read_file(path).decode()

    def _nss(self, script: str, *, stdin: str = "") -> None:
        result = self._container.sh(script, stdin=stdin.encode())
        if not result.ok:
            raise PeerError(PeerErrorCode.IPSEC_REJECTED, f"exit {result.exit_code}")


def _sign_script(client_id: str) -> str:
    """The certutil command of Ikev2Configurator; the noise goes through a file, not `<()`."""
    options = shlex.join(
        [
            "-S", "-c", CA_NICKNAME, "-n", client_id,
            "-s", f"O=IKEv2 VPN,CN={client_id}", "-k", "rsa", "-g", "3072", "-v", "120",
            *_NSS, "-t", ",,",
            "--keyUsage", "digitalSignature,keyEncipherment",
            "--extKeyUsage", "serverAuth,clientAuth",
            "-8", client_id,
        ]
    )  # fmt: skip
    return (
        'noise=$(mktemp) && head -c 1024 /dev/urandom > "$noise" && '
        f'certutil -z "$noise" {options} </dev/null; code=$?; rm -f "$noise"; exit $code'
    )
