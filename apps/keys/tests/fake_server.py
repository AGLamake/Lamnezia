"""In-memory WireGuard-family container answering the commands of PeerManager, thread-safe."""

import re
import threading
import time

from amnezia_core.awg import AwgServerConfig
from amnezia_core.containers import AWG2, WgContainer
from apps.ssh.services import CommandResult, CommandRunner, RemoteHost, RemoteProcess
from apps.ssh.tests.fakes import TIMEOUTS, result

_LOCKED_MARKER = "lamnezia-locked"
_READ = re.compile(r"cat -- ([\w/.-]+)")
_WRITE_TARGET = re.compile(r"target=([\w/.-]+)")
_MISSING_EXIT = 66


class FakeAwgServer:
    """Files of the container, the live peers of its interface and the server lock.

    Live peers remember their handshake time, so a test can see who was reconnected.
    """

    def __init__(
        self, files: dict[str, str], *, kind: WgContainer = AWG2, read_delay: float = 0.0
    ) -> None:
        self.kind = kind
        self.files = dict(files)
        self.live: dict[str, int] = {}
        self.commands: list[str] = []
        self.read_delay = read_delay
        self.lock = threading.Lock()
        self._state = threading.Lock()
        self.sync()

    def host(self) -> RemoteHost:
        return RemoteHost(_Runner(self), TIMEOUTS)

    def sync(self) -> None:
        """syncconf: keep live peers that stay, add new ones without a handshake."""
        peers = AwgServerConfig.parse(self.files[self.kind.config_path]).peers
        self.live = {peer.public_key: self.live.get(peer.public_key, 0) for peer in peers}

    def dump(self) -> str:
        interface = self.kind.interface
        lines = [f"{interface}\tprivate\tpublic\t55424\toff"]
        lines += [
            f"{interface}\t{key}\t(none)\t(none)\t(none)\t{handshake}\t0\t0\toff"
            for key, handshake in self.live.items()
        ]
        return "\n".join(lines) + "\n"

    def handle(self, command: str, stdin: bytes) -> CommandResult:
        with self._state:
            self.commands.append(command)
            if "syncconf" in command:
                self.sync()
                return result()
            if "show all dump" in command:
                return result(stdout=self.dump().encode())
            if (target := _WRITE_TARGET.search(command)) is not None:
                self.files[target[1]] = stdin.decode()
                return result()
            if (read := _READ.search(command)) is not None:
                text = self.files.get(read[1])
                if text is None:
                    return result(_MISSING_EXIT)
                content = text.encode()
            else:
                return result()
        # Let other threads run between a read and the following write.
        time.sleep(self.read_delay)
        return result(stdout=content)


class _LockHolder(RemoteProcess):
    def __init__(self, server: FakeAwgServer) -> None:
        self._server = server
        self._held = False

    def read_line(self, timeout: float) -> str | None:
        self._held = self._server.lock.acquire(timeout=timeout)
        return _LOCKED_MARKER if self._held else None

    def close_stdin(self) -> None:
        if self._held:
            self._held = False
            self._server.lock.release()

    def wait(self, timeout: float) -> int:
        return 0

    def close(self) -> None:
        self.close_stdin()


class _Runner(CommandRunner):
    def __init__(self, server: FakeAwgServer) -> None:
        self._server = server

    def run(
        self, command: str, *, stdin: bytes = b"", timeout: float | None = None
    ) -> CommandResult:
        if command == "id -u":
            return result(stdout=b"0\n")
        return self._server.handle(command, stdin)

    def start(self, command: str) -> RemoteProcess:
        return _LockHolder(self._server)
