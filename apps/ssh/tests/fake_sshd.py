"""In-process SSH server for tests: real paramiko protocol, commands answered in Python."""

import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import paramiko
from paramiko.common import AUTH_FAILED, AUTH_SUCCESSFUL, OPEN_SUCCEEDED

USERNAME = "lamnezia"
PASSWORD = "secret"

# Receives the command and the channel; must send output and an exit status.
CommandHandler = Callable[[str, paramiko.Channel], None]


def echo_handler(command: str, channel: paramiko.Channel) -> None:
    """`cat` echoes stdin, `exit N` exits with N, `sleep` never answers, anything else is echoed.

    `hold` prints a line and runs until stdin closes, like the server lock holder.
    """
    if command == "hold":
        channel.sendall(b"held\n")
        while channel.recv(65536):
            pass
        channel.send_exit_status(0)
    elif command == "cat":
        data = b""
        while chunk := channel.recv(65536):
            data += chunk
        channel.sendall(data)
        channel.send_exit_status(0)
    elif command.startswith("exit "):
        channel.sendall_stderr(b"failed\n")
        channel.send_exit_status(int(command.split()[1]))
    elif command != "sleep":
        channel.sendall(command.encode())
        channel.send_exit_status(0)


class _Server(paramiko.ServerInterface):
    def __init__(self, handler: CommandHandler, authorized_key: paramiko.PKey | None) -> None:
        self._handler = handler
        self._authorized_key = authorized_key

    def get_allowed_auths(self, username: str) -> str:
        return "password,publickey"

    def check_auth_publickey(self, username: str, key: paramiko.PKey) -> int:
        if username == USERNAME and key == self._authorized_key:
            return AUTH_SUCCESSFUL
        return AUTH_FAILED

    def check_auth_password(self, username: str, password: str) -> int:
        if (username, password) == (USERNAME, PASSWORD):
            return AUTH_SUCCESSFUL
        return AUTH_FAILED

    def check_channel_request(self, kind: str, chanid: int) -> int:
        return OPEN_SUCCEEDED

    def check_channel_exec_request(self, channel: paramiko.Channel, command: bytes) -> bool:
        threading.Thread(
            target=self._handler, args=(command.decode(), channel), daemon=True
        ).start()
        return True


@contextmanager
def fake_sshd(
    host_key: paramiko.PKey,
    handler: CommandHandler = echo_handler,
    authorized_key: paramiko.PKey | None = None,
) -> Iterator[int]:
    """Serve on a free local port and yield the port."""
    listener = socket.create_server(("127.0.0.1", 0))
    transports: list[paramiko.Transport] = []

    def serve() -> None:
        while True:
            try:
                sock, _ = listener.accept()
            except OSError:
                return
            transport = paramiko.Transport(sock)
            transport.add_server_key(host_key)
            transports.append(transport)
            try:
                transport.start_server(server=_Server(handler, authorized_key))
            except (paramiko.SSHException, EOFError):
                continue  # the client hung up during the handshake, e.g. a rejected host key

    threading.Thread(target=serve, daemon=True).start()
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()
        for transport in transports:
            transport.close()
