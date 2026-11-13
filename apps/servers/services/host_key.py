"""The SSH host key the panel trusts for a server, and forgetting it after a reinstall or a move."""

from django.db import transaction

from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.servers.models import Server
from apps.ssh.models import KnownHost
from apps.ssh.services import KnownHostVerifier

FORGET_HOST_KEY = "servers.forget_host_key"


def trusted_host_key(server: Server) -> KnownHost | None:
    return KnownHostVerifier().trusted(server.host, server.port)


@transaction.atomic
def forget_host_key(server: Server, user: User, *, ip: str | None = None) -> bool:
    """Forget the saved fingerprint; the next connection trusts the new one. False if none."""
    return _forget(server, server.host, server.port, user, ip)


@transaction.atomic
def forget_old_address(
    server: Server, host: str, port: int, user: User, *, ip: str | None = None
) -> bool:
    """After the address of the server changed, forget the fingerprint of the old host:port.

    No other server can be at that address: host and port are unique. False if nothing is
    forgotten.
    """
    if (host, port) == (server.host, server.port):
        return False
    return _forget(server, host, port, user, ip)


def _forget(server: Server, host: str, port: int, user: User, ip: str | None) -> bool:
    known = KnownHostVerifier().forget(host, port)
    if known is None:
        return False
    data: dict[str, str | int] = {"key_type": known.key_type, "fingerprint": known.fingerprint}
    if (host, port) != (server.host, server.port):
        data |= {"host": host, "port": port}
    audit.log(FORGET_HOST_KEY, actor=user, target=server, data=data, ip=ip)
    return True
