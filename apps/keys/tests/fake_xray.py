"""In-memory XRay container answering the commands of XrayClientManager."""

import json
import re

from amnezia_core.xray import XRAY_CONFIG_PATH, XRAY_DIR
from apps.ssh.services import CommandResult, CommandRunner, RemoteHost, RemoteProcess
from apps.ssh.tests.fakes import TIMEOUTS, FakeProcess, result

_READ = re.compile(r"cat -- ([\w/.-]+)")
_WRITE_TARGET = re.compile(r"target=([\w/.-]+)")
_MISSING_EXIT = 66
_REJECTED_EXIT = 65
STAGED = f"{XRAY_DIR}/server.new.json"
BACKUP = f"{XRAY_CONFIG_PATH}.bak"


class FakeXrayServer:
    """Files of the container and whether xray runs. `xray -test` accepts any valid JSON.

    `rejects` makes `xray -test` fail; `starts` decides whether xray comes up after a restart
    with a config other than `good_config` (the one it had before the test).
    """

    def __init__(self, files: dict[str, str]) -> None:
        self.files = dict(files)
        self.restarts = 0
        self.rejects = False
        self.starts = True
        self.running = True
        self.good_config = self.files[XRAY_CONFIG_PATH]
        self.commands: list[str] = []

    def host(self) -> RemoteHost:
        return RemoteHost(_Runner(self), TIMEOUTS)

    def handle(self, command: str, stdin: bytes) -> CommandResult:  # noqa: PLR0911
        self.commands.append(command)
        if (target := _WRITE_TARGET.search(command)) is not None:
            self.files[target[1]] = stdin.decode()
            return result()
        if "xray -test" in command:
            return self._swap()
        if "docker restart" in command:
            self.restarts += 1
            self.running = self.starts or self.files[XRAY_CONFIG_PATH] == self.good_config
            return result()
        if "pgrep xray" in command:
            return result(0 if self.running else 1)
        if f"[ -f {BACKUP} ]" in command:
            return result(0 if BACKUP in self.files else 1)
        if f"cp {BACKUP}" in command:
            self.files[XRAY_CONFIG_PATH] = self.files[BACKUP]
            return result()
        if f"rm -f {BACKUP}" in command:
            self.files.pop(BACKUP, None)
            return result()
        if (read := _READ.search(command)) is not None:
            text = self.files.get(read[1])
            return result(_MISSING_EXIT) if text is None else result(stdout=text.encode())
        return result()

    def _swap(self) -> CommandResult:
        staged = self.files.pop(STAGED)
        json.loads(staged)
        if self.rejects:
            return result(_REJECTED_EXIT)
        self.files[BACKUP] = self.files[XRAY_CONFIG_PATH]
        self.files[XRAY_CONFIG_PATH] = staged
        return result()


class _Runner(CommandRunner):
    def __init__(self, server: FakeXrayServer) -> None:
        self._server = server

    def run(
        self, command: str, *, stdin: bytes = b"", timeout: float | None = None
    ) -> CommandResult:
        if command == "id -u":
            return result(stdout=b"0\n")
        return self._server.handle(command, stdin)

    def start(self, command: str) -> RemoteProcess:
        return FakeProcess(["lamnezia-locked"])
