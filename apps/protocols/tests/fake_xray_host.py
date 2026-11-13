import json
import re
import shlex
from dataclasses import dataclass, field

from amnezia_core.xray import XRAY_CONFIG_PATH, XRAY_CONTAINER, XRAY_DIR
from apps.protocols.tests.fakes import FakeDebianServer
from apps.ssh.services import CommandResult
from apps.ssh.tests.fakes import result

START_SCRIPT = "/opt/amnezia/start.sh"
PLACEHOLDER = b"#!/bin/bash\ntail -f /dev/null\n"
OWN_CLIENT = "6f0c3e52-1b7a-4c8e-9d2f-0a1b2c3d4e5f"
PRIVATE_KEY = "uGQ5Zl0n6xkVYPWk5aM4lJx2bS7cT8dE9fG0hI1jK2M"
PUBLIC_KEY = "Z84J2IelR9ch3k8VtlVhhs5ycBUlXA7wHBWcBrjqnAw"
SHORT_ID = "a1b2c3d4e5f60718"
GENERATED = {
    f"{XRAY_DIR}/xray_uuid.key": OWN_CLIENT,
    f"{XRAY_DIR}/xray_short_id.key": SHORT_ID,
    f"{XRAY_DIR}/xray_private.key": PRIVATE_KEY,
    f"{XRAY_DIR}/xray_public.key": PUBLIC_KEY,
}
_DOCKER_COMMANDS = {
    "ps", "build", "run", "inspect", "start", "restart", "exec", "rename", "stop", "rm", "image"
}  # fmt: skip
PREVIOUS = f"{XRAY_CONTAINER}-previous"
STAGED = f"{XRAY_DIR}/server.new.json"
BACKUP = f"{XRAY_CONFIG_PATH}.bak"
_READ = re.compile(r"^\[ -f (\S+) \] \|\| exit (\d+); cat -- \S+$")


@dataclass
class FakeXrayHost(FakeDebianServer):
    """A ready Debian host running the docker commands of the XRay installer.

    configure_container.sh creates the keys that are missing, like the real one; `volume` is the
    data volume, which outlives the container. xray runs if start.sh starts it and server.json
    is valid JSON.
    """

    commands: set[str] = field(default_factory=lambda: {"docker", "fuser", "lsof"})
    networks: set[str] = field(default_factory=lambda: {"bridge", "amnezia-dns-net"})
    port_users: str = ""
    keygen_fails: bool = False
    container_state: str = ""
    container_networks: set[str] = field(default_factory=set)
    start_script: bytes = b""
    volume: dict[str, bytes] = field(default_factory=dict)
    host_files: dict[str, bytes] = field(default_factory=dict)
    run_arguments: list[str] = field(default_factory=list)
    xray_up: bool = False
    xray_starts: bool = True
    tun: bool = False
    container_scripts: list[str] = field(default_factory=list)
    log_driver: str = "none"
    # The container set aside under PREVIOUS: (state, start.sh, `docker run` arguments).
    previous: tuple[str, bytes, list[str]] | None = None
    restarts: int = 0

    def run(  # noqa: PLR0911
        self, command: str, *, stdin: bytes = b"", timeout: float | None = None
    ) -> CommandResult:
        argv = shlex.split(command)
        if argv[:2] != ["sh", "-c"]:
            return super().run(command, stdin=stdin, timeout=timeout)
        script = argv[2]
        words = shlex.split(script) if script.startswith("docker ") else []
        if words[:4] == ["docker", "exec", "-i", XRAY_CONTAINER]:
            return self._in_container(words[6], stdin)
        if words[:3] == ["docker", "network", "connect"]:
            self.container_networks.add(words[3])
            return result()
        if words[1:2] and words[1] in _DOCKER_COMMANDS:
            return self._docker(words)
        if script.startswith("lsof "):
            return result(stdout=self.port_users.encode())
        if "target=" in script:
            self.host_files[_target(script)] = stdin
            return result()
        return super().run(command, stdin=stdin)

    def _docker(self, words: list[str]) -> CommandResult:  # noqa: C901, PLR0911
        match words[1]:
            case "ps":
                if f"name=^{PREVIOUS}$" in words:
                    state = self.previous[0] if self.previous else ""
                    return result(stdout=f"{state}\n".encode() if state else b"")
                return result(stdout=f"{self.container_state}\n".encode())
            case "rename" | "stop" | "rm":
                return self._move(words)
            case "image":
                return result()
            case "build":
                assert f"/opt/amnezia/{XRAY_CONTAINER}/Dockerfile" in self.host_files
                return result()
            case "run":
                return self._run_container(words)
            case "inspect" if "{{.HostConfig.LogConfig.Type}}" in words:
                return result(stdout=f"{self.log_driver}\n".encode())
            case "inspect":
                networks: dict[str, dict[str, str]] = {n: {} for n in self.container_networks}
                return result(stdout=json.dumps(networks).encode())
            case "start" | "restart":
                self.restarts += words[1] == "restart"
                self.container_state, self.xray_up = "running", False
                self._start_xray()
                return result()
            case "exec":
                assert words[2] == "-d"
                self._start_xray()
                return result()
        raise AssertionError(f"unexpected docker command: {words}")

    def _run_container(self, words: list[str]) -> CommandResult:
        assert not self.container_state, "the container name is taken"
        self.run_arguments = words
        self.log_driver = words[words.index("--log-driver") + 1]
        self.container_state = "running"
        self.container_networks = {"bridge"}
        self.start_script = PLACEHOLDER
        self.xray_up = False
        return result(stdout=b"0123456789ab\n")

    def _move(self, words: list[str]) -> CommandResult:
        """`docker rename`, `stop` and `rm -f` of the container or of the one set aside."""
        command, name = words[1], words[2] if words[1] == "rename" else words[-1]
        if command == "rename" and name == XRAY_CONTAINER:
            assert words[3] == PREVIOUS and self.previous is None
            self.previous = (self.container_state, self.start_script, self.run_arguments)
            self.container_state, self.start_script, self.run_arguments = "", b"", []
            self.xray_up = False
        elif command == "rename":
            assert self.previous and not self.container_state, "cannot rename back"
            self.container_state, self.start_script, self.run_arguments = self.previous
            self.previous = None
        elif name == PREVIOUS:
            assert self.previous
            self.previous = None if command == "rm" else ("exited", *self.previous[1:])
        elif command == "rm":
            self.container_state, self.start_script, self.xray_up = "", b"", False
        else:
            self.container_state, self.xray_up = "exited", False
        return result()

    def _in_container(self, script: str, stdin: bytes) -> CommandResult:  # noqa: PLR0911
        assert self.container_state == "running", "scripts run only in a running container"
        self.container_scripts.append(script)
        files = self._files()
        if (read := _READ.match(script)) is not None:
            text = files.get(read[1])
            return result(int(read[2])) if text is None else result(stdout=text)
        if script.startswith("[ -f "):
            return result(0 if shlex.split(script)[2] in files else 1)
        if "target=" in script:
            path = _target(script)
            if path == START_SCRIPT:
                self.start_script = stdin
            else:
                self.volume[path] = stdin
            return result()
        if (replaced := self._replace_config(script)) is not None:
            return replaced
        if "xray uuid" in script:
            return self._configure()
        if "mknod /dev/net/tun" in script:
            self.tun = True
            return result()
        if "pgrep xray" in script:
            return result(0 if self.xray_up else 1)
        raise AssertionError(f"unexpected container script: {script}")

    def _replace_config(self, script: str) -> CommandResult | None:
        """The checked swap of server.json and its rollback; None for other scripts."""
        if "xray -test" in script:
            staged = self.volume.pop(STAGED)
            json.loads(staged)
            self.volume[BACKUP] = self.volume[XRAY_CONFIG_PATH]
            self.volume[XRAY_CONFIG_PATH] = staged
        elif script == f"cp {BACKUP} {XRAY_CONFIG_PATH}":
            self.volume[XRAY_CONFIG_PATH] = self.volume[BACKUP]
        elif script == f"rm -f {BACKUP}":
            self.volume.pop(BACKUP, None)
        else:
            return None
        return result()

    def _configure(self) -> CommandResult:
        if self.keygen_fails:
            return result(stdout=b"amnezia_xray_keygen=failed\n")
        for path, value in GENERATED.items():
            if not self.volume.get(path, b"").strip():
                self.volume[path] = f"{value}\n".encode()
        return result()

    def _files(self) -> dict[str, bytes]:
        return {**self.volume, START_SCRIPT: self.start_script}

    def _start_xray(self) -> None:
        if not self.xray_starts or b"xray -config" not in self.start_script:
            return
        try:
            json.loads(self.volume.get(XRAY_CONFIG_PATH, b""))
        except ValueError:
            return
        self.xray_up = True


def _target(script: str) -> str:
    return shlex.split(script.split("target=", 1)[1].split("\n", 1)[0])[0]
