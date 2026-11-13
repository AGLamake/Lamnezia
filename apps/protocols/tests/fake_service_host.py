import json
import shlex
from dataclasses import dataclass, field

from amnezia_core.services import (
    DNS_CONTAINER,
    DNS_FORWARD_RECORDS_PATH,
    ONION_PATH,
    SFTP_CONTAINER,
    TOR_CONTAINER,
)
from apps.protocols.tests.fakes import FakeDebianServer
from apps.ssh.services import CommandResult
from apps.ssh.tests.fakes import result

ONION = "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwx.onion"
# upstream remove_container.sh: one docker command per line
_SCRIPT_LINE_END = ";\\\n"
# What `docker ps` shows for the images of the services.
_PORTS = {"amnezia-dns": "53/tcp, 53/udp", TOR_CONTAINER: "80/tcp"}
# forward-records.conf of the panel's DNS image; the app's image has no log lines in it.
PANEL_RECORDS = "verbosity: 0\nlog-queries: no\nlog-replies: no\n"
APP_RECORDS = "forward-zone:\n   name: .\n"
_MISSING_FILE = 66


@dataclass
class Container:
    arguments: list[str]
    state: str = "running"
    networks: set[str] = field(default_factory=lambda: {"bridge"})
    files: dict[str, str] = field(default_factory=dict)


@dataclass
class FakeServiceHost(FakeDebianServer):
    """A ready Debian host running the docker commands of the service installers.

    `containers` are those docker knows, by name; `images` those built. Tor creates the onion
    address unless `tor_starts` is off; a new container exits at once while `fails_to_start`.
    """

    commands: set[str] = field(default_factory=lambda: {"docker", "fuser", "lsof"})
    networks: set[str] = field(default_factory=lambda: {"bridge", "amnezia-dns-net"})
    containers: dict[str, Container] = field(default_factory=dict)
    images: set[str] = field(default_factory=set)
    host_files: dict[str, bytes] = field(default_factory=dict)
    tor_starts: bool = True
    fails_to_start: bool = False
    port_users: str = ""

    def run(
        self, command: str, *, stdin: bytes = b"", timeout: float | None = None
    ) -> CommandResult:
        argv = shlex.split(command)
        if argv[:2] != ["sh", "-c"]:
            return super().run(command, stdin=stdin, timeout=timeout)
        script = argv[2]
        if _SCRIPT_LINE_END in script:
            for line in script.split(_SCRIPT_LINE_END):
                self.run(shlex.join(["sh", "-c", line.strip().rstrip(";")]))
            return result()
        if script.startswith("lsof "):
            return result(stdout=self.port_users.encode())
        if "target=" in script:
            self.host_files[shlex.split(script.split("target=", 1)[1])[0]] = stdin
            return result()
        words = shlex.split(script.removeprefix("sudo "))
        if (
            words[:1] == ["docker"]
            and words[1:2] != ["--version"]
            and words[1:3]
            not in (
                ["network", "ls"],
                ["network", "create"],
            )
        ):
            return self._docker(words)
        return super().run(command, stdin=stdin)

    def _docker(self, words: list[str]) -> CommandResult:  # noqa: C901, PLR0911, PLR0912
        match words[1:]:
            case ["ps", *_]:
                return self._ps(words)
            case ["build", *_]:
                self.images.add(words[words.index("-t") + 1])
                return result()
            case ["run", *_]:
                name = words[words.index("--name") + 1]
                assert name in self.images and name not in self.containers
                self.containers[name] = Container(words)
                if self.fails_to_start:
                    self.containers[name].state = "exited"
                if name == DNS_CONTAINER:
                    self.containers[name].files[DNS_FORWARD_RECORDS_PATH] = PANEL_RECORDS
                if "--network" in words:
                    self.containers[name].networks = {words[words.index("--network") + 1]}
                return result(stdout=b"0123456789ab\n")
            case ["inspect", *_]:
                return self._inspect(words)
            case ["network", "connect", *_]:
                self.containers[words[4]].networks.add(words[3])
                return result()
            case ["exec", "-i", *_]:
                return self._in_container(words[3], words[6])
            case ["start", name]:
                self.containers[name].state = "running"
                return result()
            case ["stop", name]:
                if name in self.containers:
                    self.containers[name].state = "exited"
                return result()
            case ["rename", name, new_name]:
                assert new_name not in self.containers
                self.containers[new_name] = self.containers.pop(name)
                return result()
            case ["rm", "-fv" | "-f", name]:
                self.containers.pop(name, None)
                return result()
            case ["rmi", name]:
                self.images.discard(name)
                return result()
        raise AssertionError(f"unexpected docker command: {words}")

    def _ps(self, words: list[str]) -> CommandResult:
        if "--filter" in words:
            name = words[words.index("--filter") + 1].removeprefix("name=^").removesuffix("$")
            container = self.containers.get(name)
            return result(stdout=f"{container.state}\n".encode() if container else b"")
        lines = [
            f"{name} {self._ports(name, container)}"
            for name, container in self.containers.items()
            if container.state == "running"
        ]
        return result(stdout="".join(f"{line}\n" for line in lines).encode())

    def _ports(self, name: str, container: Container) -> str:
        if "-p" in container.arguments:
            published = container.arguments[container.arguments.index("-p") + 1]
            port, _, inner = published.partition(":")
            return f"0.0.0.0:{port}->{inner}"
        return _PORTS.get(name, "")

    def _inspect(self, words: list[str]) -> CommandResult:
        name = words[-1]
        container = self.containers[name]
        if "{{.Config.Cmd}}" in words:
            # docker prints the command after the image as a Go list
            image = len(container.arguments) - container.arguments[::-1].index(name)
            arguments = container.arguments[image:]
            return result(stdout=f"[{' '.join(arguments)}]\n".encode())
        if "{{.HostConfig.LogConfig.Type}}" in words:
            arguments = container.arguments
            logs = "--log-driver" in arguments
            driver = arguments[arguments.index("--log-driver") + 1] if logs else "json-file"
            return result(stdout=f"{driver}\n".encode())
        networks: dict[str, dict[str, str]] = {n: {} for n in container.networks}
        return result(stdout=json.dumps(networks).encode())

    def _in_container(self, name: str, script: str) -> CommandResult:
        container = self.containers[name]
        assert container.state == "running"
        if name == DNS_CONTAINER:
            assert DNS_FORWARD_RECORDS_PATH in script, script
            records = container.files.get(DNS_FORWARD_RECORDS_PATH)
            return result(_MISSING_FILE) if records is None else result(stdout=records.encode())
        assert name == TOR_CONTAINER, f"unexpected script in {name}: {script}"
        assert ONION_PATH in script
        return result(stdout=f"{ONION}\n".encode()) if self.tor_starts else result(1)

    def sftp_password(self) -> str:
        arguments = self.containers[SFTP_CONTAINER].arguments
        return arguments[-1].split(":")[1]
