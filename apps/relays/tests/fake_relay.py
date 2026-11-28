"""A fake relay server: a ready Debian host that runs the docker commands of the relay."""

import json
import re
import shlex
from dataclasses import dataclass, field

from apps.protocols.tests.fakes import FakeDebianServer
from apps.relays.services.container import FOLDER, NAME, VOLUME
from apps.ssh.services import CommandResult, Stdin
from apps.ssh.tests.fakes import result, stdin_bytes

_MISSING_EXIT = re.compile(r"\|\| exit (\d+);")
_VERBS = {"ps", "build", "run", "rm", "rmi", "restart", "inspect", "exec", "volume"}


@dataclass
class FakeRelayServer(FakeDebianServer):
    """`others` are lines of `docker ps` for containers that are not the relay.

    `files` of a container started with the relay volume are the files of `volume`: they stay
    when the container goes; `volume` is None while docker has no such volume.
    `inspect_fails` makes `docker inspect` fail as a busy docker daemon would.
    """

    commands: set[str] = field(default_factory=lambda: {"docker", "fuser", "lsof"})
    networks: set[str] = field(default_factory=lambda: {"bridge", "amnezia-dns-net"})
    port_users: str = ""
    others: list[str] = field(default_factory=list)
    state: str = ""
    ports: set[int] = field(default_factory=set)
    run_arguments: list[str] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)
    volume: dict[str, bytes] | None = None
    host_files: dict[str, bytes] = field(default_factory=dict)
    has_image: bool = False
    docker_calls: list[str] = field(default_factory=list)
    ifb_loaded: bool = False
    inspect_fails: bool = False

    def run(
        self, command: str, *, stdin: Stdin = b"", timeout: float | None = None
    ) -> CommandResult:
        stdin = stdin_bytes(stdin)
        argv = shlex.split(command)
        script = argv[2] if argv[:2] == ["sh", "-c"] else ""
        words = shlex.split(script) if script.startswith("docker ") else []
        if words and words[1] in _VERBS:
            self.docker_calls.append(words[1])
            return self._docker(words, stdin)
        if script.startswith("modprobe ifb"):
            self.ifb_loaded = True
            return result()
        if script.startswith("lsof "):
            return result(stdout=self.port_users.encode())
        if "target=" in script:
            self.host_files[_target(script)] = stdin
            return result()
        return super().run(command, stdin=stdin, timeout=timeout)

    def _docker(self, words: list[str], stdin: bytes) -> CommandResult:  # noqa: PLR0911
        verb = words[1]
        if verb == "ps" and "--filter" in words:
            return result(stdout=f"{self.state}\n".encode() if self.state else b"")
        if verb == "ps":
            mine = ", ".join(f"0.0.0.0:{port}->{port}/udp" for port in sorted(self.ports))
            lines = [*self.others, *([f"{NAME} {mine}"] if self.state else [])]
            return result(stdout="".join(f"{line}\n" for line in lines).encode())
        if verb == "build":
            assert f"/opt/amnezia/{NAME}/Dockerfile" in self.host_files
            self.has_image = True
            return result()
        if verb == "run":
            return self._run(words)
        if verb in {"rm", "rmi", "volume"}:
            return self._remove(words)
        if verb == "restart":
            assert self.state
            return result()
        if verb == "inspect" and self.inspect_fails:
            return result(exit_code=1, stderr=b"Cannot connect to the Docker daemon")
        if verb == "inspect":
            bindings = {f"{port}/udp": [{"HostPort": str(port)}] for port in self.ports}
            return result(stdout=json.dumps(bindings).encode())
        if words[:4] == ["docker", "exec", "-i", NAME]:
            return self._in_container(words[6], stdin)
        raise AssertionError(f"unexpected docker command: {words}")

    def _run(self, words: list[str]) -> CommandResult:
        assert not self.state and self.has_image
        self.state, self.files, self.run_arguments = "running", {}, words
        if f"{VOLUME}:{FOLDER}" in words:
            self.volume = {} if self.volume is None else self.volume
            self.files = self.volume
        self.ports = {
            int(words[index + 1].split(":")[0]) for index, word in enumerate(words) if word == "-p"
        }
        return result(stdout=b"0123456789ab\n")

    def _remove(self, words: list[str]) -> CommandResult:
        """`docker rm` of the container, `rmi` of the image or `volume rm`."""
        if words[1] == "rm":
            self.state, self.ports, self.files = "", set(), {}
        elif words[1] == "rmi":
            self.has_image = False
        else:
            assert words[2] == "rm", words
            self.volume = None
        return result()

    def _in_container(self, script: str, stdin: bytes) -> CommandResult:
        assert self.state == "running"
        if "target=" in script:
            self.files[_target(script)] = stdin
            return result()
        if script.startswith("rm -f -- "):
            for path in shlex.split(script.removeprefix("rm -f -- ")):
                self.files.pop(path, None)
            return result()
        if "cat -- " in script:
            content = self.files.get(script.rsplit("cat -- ", 1)[1])
            missing = _MISSING_EXIT.search(script)
            if content is None and missing:
                return result(int(missing[1]))
            return result(stdout=content or b"")
        raise AssertionError(f"unexpected container script: {script}")


def _target(script: str) -> str:
    return shlex.split(script.split("target=", 1)[1].split("\n", 1)[0])[0]
