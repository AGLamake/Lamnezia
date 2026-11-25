"""Local stand-ins for a web server, an HTTP proxy and a SOCKS5 proxy, for tests of the proxies."""

import json
import select
import socket
import socketserver
import struct
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_SOCKS_VERSION = 5
_NO_AUTH, _PASSWORD = 0, 2
_DOMAIN, _IPV4 = 3, 1


@dataclass
class Seen:
    """What a stand-in received: request paths (or SOCKS targets) and credentials."""

    paths: list[str] = field(default_factory=list)
    auth: list[str] = field(default_factory=list)


class _Answering(BaseHTTPRequestHandler):
    """Answers any GET or POST with `{"ok": true}` and records the path and Proxy-Authorization."""

    seen: Seen

    def do_GET(self) -> None:
        self.seen.paths.append(self.path)
        self.seen.auth.append(self.headers.get("Proxy-Authorization", ""))
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.do_GET()

    def log_message(self, *args: object) -> None:
        return None


@contextmanager
def serving(handler: type[BaseHTTPRequestHandler]) -> Iterator[int]:
    """Serve `handler` on 127.0.0.1 until the block ends; yields the port.

    A handler must read the request body: Windows resets a socket closed with unread data.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    with _running(server):
        yield server.server_address[1]


@contextmanager
def web_server() -> Iterator[tuple[int, Seen]]:
    """A web server on 127.0.0.1; also serves as an HTTP proxy for plain http:// targets."""
    seen = Seen()
    handler = type("Handler", (_Answering,), {"seen": seen})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    with _running(server):
        yield server.server_address[1], seen


def _read(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data


class _Socks5(socketserver.BaseRequestHandler):
    """SOCKS5 CONNECT with an optional login; every name leads to 127.0.0.1."""

    seen: Seen
    password: bool
    accept: bool

    def handle(self) -> None:
        client: socket.socket = self.request
        _, count = _read(client, 2)
        _read(client, count)
        if self.password:
            client.sendall(bytes([_SOCKS_VERSION, _PASSWORD]))
            _, size = _read(client, 2)
            user = _read(client, size).decode()
            password = _read(client, _read(client, 1)[0]).decode()
            self.seen.auth.append(f"{user}:{password}")
            client.sendall(b"\x01\x00" if self.accept else b"\x01\x01")
            if not self.accept:
                return
        else:
            client.sendall(bytes([_SOCKS_VERSION, _NO_AUTH]))
        _, _, _, kind = _read(client, 4)
        if kind == _DOMAIN:
            host = _read(client, _read(client, 1)[0]).decode()
        else:
            host = socket.inet_ntoa(_read(client, 4))
        (port,) = struct.unpack("!H", _read(client, 2))
        self.seen.paths.append(f"{host}:{port}")
        with socket.create_connection(("127.0.0.1", port)) as upstream:
            client.sendall(bytes([_SOCKS_VERSION, 0, 0, _IPV4]) + bytes(6))
            _relay(client, upstream)


def _relay(one: socket.socket, other: socket.socket) -> None:
    while True:
        ready, _, _ = select.select([one, other], [], [], 5)
        if not ready:
            return
        for sock in ready:
            data = sock.recv(65536)
            if not data:
                return
            (other if sock is one else one).sendall(data)


@contextmanager
def socks_proxy(*, password: bool = False, accept: bool = True) -> Iterator[tuple[int, Seen]]:
    """A SOCKS5 proxy; with `password` it asks for a login and takes it unless `accept` is off."""
    seen = Seen()
    attributes = {"seen": seen, "password": password, "accept": accept}
    handler = type("Handler", (_Socks5,), attributes)
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    with _running(server):
        yield server.server_address[1], seen


@contextmanager
def _running(server: socketserver.BaseServer) -> Iterator[None]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
