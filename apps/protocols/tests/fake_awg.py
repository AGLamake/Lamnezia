import json
import re
import shlex
from dataclasses import dataclass, field

from amnezia_core.awg import AwgServerConfig
from amnezia_core.containers import AWG2, WgContainer
from amnezia_core.keys import KeyPair
from apps.protocols.tests.fakes import FakeDebianServer
from apps.ssh.services import CommandResult
from apps.ssh.tests.fakes import result

PREVIOUS = "amnezia-awg2-previous"
START_SCRIPT = "/opt/amnezia/start.sh"
_HEREDOC = re.compile(r"<<EOF\n(?P<body>.*?)\nEOF\n", re.S)
_EMPTY_VALUE = re.compile(r"^[^=]*= *$")
_PSK = "c2VjcmV0LXByZXNoYXJlZC1rZXktZm9yLXRlc3RzLTA="
_SCRIPT_LINE_END = ";\\\n"
_MISSING_EXIT = re.compile(r"\|\| exit (\d+);")


@dataclass
class FakeAwgServer(FakeDebianServer):
    """A ready Debian host that runs the docker commands of a WireGuard-family installer.

    configure_container.sh is executed for real as far as it matters: its config heredoc is
    filled in, so tests read the file the server would have. `kind` is the container.
    """

    kind: WgContainer = AWG2
    commands: set[str] = field(default_factory=lambda: {"docker", "fuser", "lsof"})
    networks: set[str] = field(default_factory=lambda: {"bridge", "amnezia-dns-net"})
    kernel: str = "6.8.0-45-generic"
    port_users: str = ""
    build_error: bytes = b""
    run_error: bytes = b""
    container_state: str = ""
    container_networks: set[str] = field(default_factory=set)
    container_files: dict[str, bytes] = field(default_factory=dict)
    host_files: dict[str, bytes] = field(default_factory=dict)
    awg_up: bool = False
    awg_starts: bool = True
    # What `<tool>-quick up` must find in the config to bring the interface up.
    awg_refuses: str = ""
    # An old amneziawg kernel module brings the interface up without the 3.x settings.
    drops_awg3: bool = False
    has_image: bool = True
    published_port: int | None = None
    # `--log-driver` of the running container: Amnezia runs it with none.
    log_driver: str = "none"
    # The container set aside under PREVIOUS: (state, files, published port).
    previous: tuple[str, dict[str, bytes], int | None] | None = None
    docker_calls: list[str] = field(default_factory=list)
    # What `docker logs` prints; Amnezia containers run with `--log-driver none`.
    container_log: bytes = (
        b"Error response from daemon: configured logging driver does not support reading\n"
    )
    server_keys: KeyPair = field(default_factory=KeyPair.generate)

    def run(
        self, command: str, *, stdin: bytes = b"", timeout: float | None = None
    ) -> CommandResult:
        argv = shlex.split(command)
        if argv[:2] != ["sh", "-c"]:
            return super().run(command, stdin=stdin, timeout=timeout)
        script = argv[2]
        if _SCRIPT_LINE_END in script:
            # upstream remove_container.sh: one docker command per line, failures ignored
            for line in script.split(_SCRIPT_LINE_END):
                self.run(shlex.join(["sh", "-c", line.strip().rstrip(";")]))
            return result()
        words = shlex.split(script) if script.startswith("docker ") else []
        if words[:4] == ["docker", "exec", "-i", self.kind.name]:
            return self._in_container(words[6], stdin)
        docker_commands = {
            "ps": self._docker_ps,
            "build": self._docker_build,
            "run": self._docker_run,
            "rm": self._docker_rm,
            "inspect": self._docker_inspect,
            "start": self._docker_start,
            "exec": self._docker_exec,
            "image": self._docker_image,
            "stop": self._docker_stop,
            "rename": self._docker_rename,
            "restart": self._docker_restart,
            "logs": lambda words: result(stdout=self.container_log),
            "rmi": self._docker_rmi,
        }
        if words and words[1] in docker_commands:
            self.docker_calls.append(words[1])
            return docker_commands[words[1]](words)
        if words[:3] == ["docker", "network", "connect"]:
            self.container_networks.add(words[3])
            return result()
        return self._on_host(script, command, stdin)

    def _on_host(self, script: str, command: str, stdin: bytes) -> CommandResult:
        if script == "uname -r":
            return result(stdout=f"{self.kernel}\n".encode())
        if script.startswith("lsof "):
            return result(stdout=self.port_users.encode())
        if "target=" in script:
            self.host_files[_target(script)] = stdin
            return result()
        return super().run(command, stdin=stdin)

    def _in_container(self, script: str, stdin: bytes) -> CommandResult:
        assert self.container_state == "running", "scripts run only in a running container"
        if "cat -- " in script:
            return self._read(script)
        if script.startswith("[ -f "):
            return result(0 if shlex.split(script)[2] in self.container_files else 1)
        if "target=" in script:
            self.container_files[_target(script)] = stdin
            return result()
        if f"{self.kind.tool} genkey" in script:
            self._configure(script)
            return result()
        answer = self._awg_tool(script)
        if answer is None:
            raise AssertionError(f"unexpected container script: {script}")
        return answer

    def _awg_tool(self, script: str) -> CommandResult | None:
        """`awg` and `awg-quick` in the container; None for other scripts."""
        if f"{self.kind.tool} show {self.kind.interface}" in script:
            return self._show(script)
        if script.startswith(f"{self._quick} "):
            return self._awg_quick(script)
        if f"{self.kind.tool} syncconf" in script:
            return result()
        if script == f"{self.kind.tool} show all dump":
            return result(stdout=self._dump().encode())
        return None

    def _show(self, script: str) -> CommandResult:
        """`awg show`: up while AWG runs; prints the header protection key of AWG 3.x."""
        if "header protection key" not in script:
            return result(0 if self.awg_up else 1)
        config = self.container_files.get(self.kind.config_path, b"")
        applied = b"HeaderProtectionKey" in config and not self.drops_awg3
        return result(0 if self.awg_up and applied else 1)

    def _dump(self) -> str:
        """`show all dump` after syncconf: the interface has the peers of the config."""
        interface = self.kind.interface
        config = AwgServerConfig.parse(self.container_files[self.kind.config_path].decode())
        lines = [f"{interface}\tprivate\tpublic\t55424\toff"]
        lines += [
            f"{interface}\t{peer.public_key}\t(none)\t(none)\t(none)\t0\t0\t0\toff"
            for peer in config.peers
        ]
        return "\n".join(lines) + "\n"

    def _read(self, script: str) -> CommandResult:
        content = self.container_files.get(script.rsplit("cat -- ", 1)[1])
        missing = _MISSING_EXIT.search(script)
        if content is None and missing:
            return result(int(missing[1]))
        assert content is not None, f"no such file: {script}"
        return result(stdout=content)

    def _configure(self, script: str) -> None:
        body = _HEREDOC.search(script)
        assert body, "configure_container.sh writes the config with a heredoc"
        text = body["body"].replace("$WIREGUARD_SERVER_PRIVATE_KEY", self.server_keys.private_key)
        assert "$" not in text, f"unsubstituted variable in the config:\n{text}"
        lines = [line for line in text.split("\n") if not _EMPTY_VALUE.match(line)]
        self.container_files[self.kind.config_path] = ("\n".join(lines) + "\n").encode()
        self.container_files[self.kind.psk_path] = f"{_PSK}\n".encode()

    def _awg_quick(self, script: str) -> CommandResult:
        if script.startswith(f"{self._quick} down "):
            self.awg_up = False
            return result()
        config = self.container_files[self.kind.config_path].decode()
        if self.awg_refuses and self.awg_refuses in config:
            return result(1, stderr=b"Line unrecognized\n")
        self.awg_up = True
        return result()

    def _docker_ps(self, words: list[str]) -> CommandResult:
        if "--filter" not in words:
            # what the import reads: the running containers with their published ports
            running = self.container_state == "running" and self.published_port
            line = f"{self.kind.name} 0.0.0.0:{self.published_port}->{self.published_port}/udp\n"
            return result(stdout=line.encode() if running else b"")
        name = words[words.index("--filter") + 1].removeprefix("name=^").removesuffix("$")
        state = (
            self.container_state if name == self.kind.name else self.previous and self.previous[0]
        )
        return result(stdout=f"{state}\n".encode() if state else b"")

    def _docker_build(self, words: list[str]) -> CommandResult:
        assert f"/opt/amnezia/{self.kind.name}/Dockerfile" in self.host_files
        if self.build_error:
            return result(1, stderr=self.build_error)
        self.has_image = True
        return result()

    def _docker_run(self, words: list[str]) -> CommandResult:
        if self.run_error:
            self.container_state = "created"
            return result(125, stderr=self.run_error)
        assert not self.container_state, "the container name is taken"
        assert self.has_image, "docker run needs the image"
        self.container_state = "running"
        self.container_networks = {"bridge"}
        self.container_files = {START_SCRIPT: b"#!/bin/bash\ntail -f /dev/null\n"}
        self.published_port = int(words[words.index("-p") + 1].split(":", 1)[0])
        self.log_driver = words[words.index("--log-driver") + 1]
        self.awg_up = False
        return result(stdout=b"0123456789ab\n")

    def _docker_rm(self, words: list[str]) -> CommandResult:
        if words[-1] == self.previous_name:
            self.previous = None
        else:
            self.container_state = ""
            self.container_files = {}
            self.published_port = None
        return result()

    def _docker_image(self, words: list[str]) -> CommandResult:
        return result(0 if self.has_image else 1)

    def _docker_stop(self, words: list[str]) -> CommandResult:
        if words[-1] == self.previous_name:
            assert self.previous
            self.previous = ("exited", *self.previous[1:])
        else:
            self.container_state = "exited"
        return result()

    def _docker_rename(self, words: list[str]) -> CommandResult:
        source, target = words[2:4]
        if source == self.kind.name:
            assert target == self.previous_name and self.previous is None
            self.previous = (self.container_state, self.container_files, self.published_port)
            self.container_state, self.container_files, self.published_port = "", {}, None
        else:
            assert self.previous and not self.container_state, "cannot rename back"
            self.container_state, self.container_files, self.published_port = self.previous
            self.previous = None
        return result()

    def _docker_inspect(self, words: list[str]) -> CommandResult:
        if ".State.Status" in words[3]:
            return result(stdout=f"{self.container_state} · restarts 0\n".encode())
        if ".HostConfig.LogConfig.Type" in words[3]:
            return result(stdout=f"{self.log_driver}\n".encode())
        networks: dict[str, dict[str, str]] = {name: {} for name in self.container_networks}
        return result(stdout=json.dumps(networks).encode())

    def _docker_start(self, words: list[str]) -> CommandResult:
        self.container_state = "running"
        self.awg_up = self.awg_starts and self._starts_interface(
            self.container_files.get(START_SCRIPT, b"")
        )
        return result()

    def _docker_restart(self, words: list[str]) -> CommandResult:
        assert self.container_state, "docker restart needs the container"
        return self._docker_start(words)

    def _docker_rmi(self, words: list[str]) -> CommandResult:
        assert not self.container_state, "the image is in use"
        self.has_image = False
        return result()

    def _docker_exec(self, words: list[str]) -> CommandResult:
        assert words[2] == "-d"
        self.awg_up = self.awg_starts and self._starts_interface(self.container_files[START_SCRIPT])
        return result()

    @property
    def previous_name(self) -> str:
        return f"{self.kind.name}-previous"

    @property
    def _quick(self) -> str:
        return f"{self.kind.tool}-quick"

    def _starts_interface(self, start_script: bytes) -> bool:
        return f"{self._quick} up".encode() in start_script


def _target(script: str) -> str:
    return shlex.split(script.split("target=", 1)[1].split("\n", 1)[0])[0]
