from collections.abc import Iterator
from contextlib import contextmanager

from apps.servers.models import Server
from apps.ssh.services import RemoteHost, SshConnection, SshTimeouts


@contextmanager
def connect(server: Server) -> Iterator[RemoteHost]:
    """An SSH session to the server as a root shell; closed when the block ends."""
    timeouts = SshTimeouts.from_settings()
    with SshConnection(server.credentials(), timeouts=timeouts) as connection:
        yield RemoteHost(connection, timeouts)
