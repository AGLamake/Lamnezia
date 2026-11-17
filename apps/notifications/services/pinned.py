"""HTTP connections to addresses checked in advance, so the name is not resolved again on connect.

Between the `AddressGuard` check and the connection a name could start pointing into a private
network (DNS rebinding). The opener here connects only to the checked addresses, while the Host
header and the TLS certificate check still go by the name.
"""

import http.client
import socket
import ssl
import urllib.request
from collections.abc import Sequence
from typing import Any


class PinnedAddresses:
    """The checked addresses of one host; a connection goes to the first of them that answers."""

    def __init__(self, addresses: Sequence[str]) -> None:
        if not addresses:
            raise ValueError("no address to pin")
        self.addresses = tuple(dict.fromkeys(addresses))

    def connect(self, port: int, timeout: Any) -> socket.socket:
        """A TCP connection to one of the addresses; the last error if none answers."""
        errors: list[OSError] = []
        for address in self.addresses:
            try:
                return socket.create_connection((address, port), timeout)
            except OSError as error:
                errors.append(error)
        raise errors[-1]

    def opener(self, *handlers: urllib.request.BaseHandler) -> urllib.request.OpenerDirector:
        """An opener that goes only to these addresses, past any proxy from the environment."""
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _PinnedHTTPHandler(self),
            _PinnedHTTPSHandler(self),
            *handlers,
        )


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, *, pinned: PinnedAddresses, **kwargs: Any) -> None:
        super().__init__(host, **kwargs)
        self._pinned = pinned

    def connect(self) -> None:
        self.sock = self._pinned.connect(self.port, self.timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        *,
        pinned: PinnedAddresses,
        context: ssl.SSLContext | None = None,
        **kwargs: Any,
    ) -> None:
        self._tls = context or ssl.create_default_context()
        super().__init__(host, context=self._tls, **kwargs)
        self._pinned = pinned

    def connect(self) -> None:
        sock = self._pinned.connect(self.port, self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # SNI and the certificate are checked against the name, not the address.
        self.sock = self._tls.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, pinned: PinnedAddresses) -> None:
        super().__init__()
        self._pinned = pinned

    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(self._connection, req)

    def _connection(self, host: str, **kwargs: Any) -> http.client.HTTPConnection:
        return _PinnedHTTPConnection(host, pinned=self._pinned, **kwargs)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, pinned: PinnedAddresses) -> None:
        super().__init__()
        self._pinned = pinned

    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(self._connection, req)

    def _connection(self, host: str, **kwargs: Any) -> http.client.HTTPConnection:
        return _PinnedHTTPSConnection(host, pinned=self._pinned, **kwargs)
