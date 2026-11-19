"""Other addresses of a server for keys, and the check that an address leads to that server.

A second IP at the hosting provider, or a domain on one of the server's IPs: if a provider
blocks the main address for some users, their keys can send them to another one. The check
connects to the address on the SSH port of the server and compares the host key it is shown
with the one the panel trusts for the server: the same key means the same machine. Nothing is
logged in, so no credentials go to an address that turns out to be another machine.
"""

from collections.abc import Callable
from enum import StrEnum

from django.conf import settings
from django.db import transaction
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.servers.models import Server, ServerAddress
from apps.servers.services.host_key import trusted_host_key
from apps.ssh.errors import CatalogError, ErrorDescription, SshError
from apps.ssh.services import read_host_key

CHECK_ADDRESS_KIND = "servers.check_address"
ADD_ADDRESS = "servers.add_address"
REMOVE_ADDRESS = "servers.remove_address"

HostKeyReader = Callable[[str, int, float], tuple[str, str]]


class AddressErrorCode(StrEnum):
    NO_TRUSTED_KEY = "address_no_trusted_key"
    OTHER_MACHINE = "address_other_machine"


ADDRESS_ERRORS: dict[AddressErrorCode, ErrorDescription] = {
    AddressErrorCode.NO_TRUSTED_KEY: ErrorDescription(
        gettext_lazy("The panel has not saved the SSH key of the server yet."),
        gettext_lazy("Check the server first, then check the address again."),
    ),
    AddressErrorCode.OTHER_MACHINE: ErrorDescription(
        gettext_lazy("The address leads to another machine: its SSH key is not the server's."),
        gettext_lazy("Check the address. A domain must point to one of the server's IPs."),
    ),
}


class AddressError(CatalogError[AddressErrorCode]):
    """The address does not lead to the server."""

    catalog = ADDRESS_ERRORS


class AddressCheck:
    """Whether the address leads to its server; the result is saved on the address."""

    def __init__(
        self, address: ServerAddress, ctx: OperationContext, read: HostKeyReader = read_host_key
    ) -> None:
        self._address = address
        self._ctx = ctx
        self._read = read

    def run(self) -> None:
        """Raises the reason when the address is not the server's; it is saved before."""
        try:
            self._compare()
        except (AddressError, SshError) as error:
            self._address.record_check(error.message)
            raise
        self._address.record_check()
        self._ctx.log(_("The address leads to the server."))

    def _compare(self) -> None:
        server = self._address.server
        trusted = trusted_host_key(server)
        if trusted is None:
            raise AddressError(AddressErrorCode.NO_TRUSTED_KEY)
        self._ctx.log(f"{self._address.host}:{server.port}")
        key_type, fingerprint = self._read(
            self._address.host, server.port, settings.SSH_CONNECT_TIMEOUT
        )
        self._ctx.log(f"{key_type} {fingerprint}")
        if (key_type, fingerprint) != (trusted.key_type, trusted.fingerprint):
            raise AddressError(AddressErrorCode.OTHER_MACHINE)


class ServerAddresses:
    """Adding and removing the other addresses of a server, on behalf of `actor`."""

    def __init__(self, server: Server, actor: User, ip: str | None = None) -> None:
        self.server = server
        self._actor = actor
        self._ip = ip

    @transaction.atomic
    def add(self, host: str) -> Operation:
        """Save the address and start its check; keys may take it once the check passes."""
        address = ServerAddress.objects.create(server=self.server, host=host)
        self._log(ADD_ADDRESS, host)
        return self.check(address)

    def check(self, address: ServerAddress) -> Operation:
        params = {"server_id": self.server.pk, "address_id": address.pk}
        return launch(CHECK_ADDRESS_KIND, params, user=self._actor, subject=self.server.name)

    @transaction.atomic
    def remove(self, address: ServerAddress) -> None:
        """Keys sent there keep it until they are moved to the server's own address."""
        address.delete()
        self._log(REMOVE_ADDRESS, address.host)

    def _log(self, action: str, host: str) -> None:
        audit.log(action, actor=self._actor, target=self.server, data={"host": host}, ip=self._ip)
