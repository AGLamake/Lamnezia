import json
import re
import shlex
from dataclasses import dataclass, field

from amnezia_core.openvpn import OPENVPN_CONFIG_PATH, OPENVPN_CONTAINER, OPENVPN_DIR
from apps.protocols.tests.fakes import FakeDebianServer
from apps.ssh.services import CommandResult
from apps.ssh.tests.fakes import result

START_SCRIPT = "/opt/amnezia/start.sh"
CA_CERT = f"{OPENVPN_DIR}/ca.crt"
CRL = f"{OPENVPN_DIR}/crl.pem"
# What the easy-rsa part of run_container.sh leaves in the container.
PKI_FILES = {
    CA_CERT: b"-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----\n",
    f"{OPENVPN_DIR}/AmneziaReq.crt": b"server certificate\n",
    f"{OPENVPN_DIR}/AmneziaReq.key": b"server key\n",
    f"{OPENVPN_DIR}/dh.pem": b"dh\n",
    f"{OPENVPN_DIR}/ta.key": b"ta\n",
}
_DOCKER_COMMANDS = {
    "ps", "build", "run", "inspect", "start", "restart", "exec", "rename", "stop", "rm",
    "image", "cp",
}  # fmt: skip
PREVIOUS = f"{OPENVPN_CONTAINER}-previous"
_READ = re.compile(r"^\[ -f (\S+) \] \|\| exit (\d+); cat -- \S+$")
_SERVER_CONF = re.compile(rf"cat > {OPENVPN_CONFIG_PATH} <<EOF\n(.*)EOF\n", re.DOTALL)


@dataclass
class FakeOpenVpnHost(FakeDebianServer):
    """A ready Debian host running the docker commands of the OpenVPN installer.

    `files` is the container: there is no data volume, so it is gone with the container.
    openvpn runs if start.sh starts it, the CA is there and server.conf exists.
    """

    commands: set[str] = field(default_factory=lambda: {"docker", "fuser", "lsof"})
    networks: set[str] = field(default_factory=lambda: {"bridge", "amnezia-dns-net"})
    container_networks: set[str] = field(default_factory=set)
    port_users: str = ""
    container_state: str = ""
    files: dict[str, bytes] = field(default_factory=dict)
    host_files: dict[str, bytes] = field(default_factory=dict)
    run_arguments: list[str] = field(default_factory=list)
    pki_fails: bool = False
    pki_runs: int = 0
    openvpn_up: bool = False
    openvpn_starts: bool = True
    container_scripts: list[str] = field(default_factory=list)
    # The container set aside under PREVIOUS: (state, files, `docker run` arguments).
    previous: tuple[str, dict[str, bytes], list[str]] | None = None
    copy_fails: bool = False

    def run(  # noqa: PLR0911
        self, command: str, *, stdin: bytes = b"", timeout: float | None = None
    ) -> CommandResult:
        argv = shlex.split(command)
        if argv[:2] != ["sh", "-c"]:
            return super().run(command, stdin=stdin, timeout=timeout)
        script = argv[2]
        words = shlex.split(script) if script.startswith("docker ") else []
        if words[:4] == ["docker", "exec", "-i", OPENVPN_CONTAINER]:
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
            case "ps" if f"name=^{PREVIOUS}$" in words:
                state = self.previous[0] if self.previous else ""
                return result(stdout=f"{state}\n".encode() if state else b"")
            case "ps":
                return result(stdout=f"{self.container_state}\n".encode())
            case "rename" | "stop" | "rm":
                return self._move(words)
            case "image":
                return result()
            case "cp":
                return self._copy(words)
            case "build":
                assert f"/opt/amnezia/{OPENVPN_CONTAINER}/Dockerfile" in self.host_files
                return result()
            case "run":
                assert not self.container_state, "the container name is taken"
                self.run_arguments = words
                self.container_state = "running"
                self.container_networks = {"bridge"}
                return result(stdout=b"0123456789ab\n")
            case "inspect":
                networks: dict[str, dict[str, str]] = {n: {} for n in self.container_networks}
                return result(stdout=json.dumps(networks).encode())
            case "start" | "restart":
                self.container_state = "running"
                self._start_openvpn()
                return result()
            case "exec":
                assert words[2] == "-d"
                self._start_openvpn()
                return result()
        raise AssertionError(f"unexpected docker command: {words}")

    def _move(self, words: list[str]) -> CommandResult:
        """`docker rename`, `stop` and `rm -f` of the container or of the one set aside."""
        command, name = words[1], words[2] if words[1] == "rename" else words[-1]
        if command == "rename" and name == OPENVPN_CONTAINER:
            assert words[3] == PREVIOUS and self.previous is None
            self.previous = (self.container_state, self.files, self.run_arguments)
            self.container_state, self.files, self.run_arguments = "", {}, []
            self.openvpn_up = False
        elif command == "rename":
            assert self.previous and not self.container_state, "cannot rename back"
            self.container_state, self.files, self.run_arguments = self.previous
            self.previous = None
        elif name == PREVIOUS:
            assert self.previous
            self.previous = None if command == "rm" else ("exited", *self.previous[1:])
        elif command == "rm":
            self.container_state, self.files, self.openvpn_up = "", {}, False
        else:
            self.container_state, self.openvpn_up = "exited", False
        return result()

    def _copy(self, words: list[str]) -> CommandResult:
        """`docker cp PREVIOUS:dir - | docker cp - CONTAINER:parent`, the folder as it is."""
        assert (
            words[2] == f"{PREVIOUS}:{OPENVPN_DIR}"
            and words[-1] == f"{OPENVPN_CONTAINER}:/opt/amnezia"
        )
        assert self.previous and self.container_state == "running"
        if not self.copy_fails:
            prefix = f"{OPENVPN_DIR}/"
            self.files.update(
                {path: data for path, data in self.previous[1].items() if path.startswith(prefix)}
            )
        return result()

    def _in_container(self, script: str, stdin: bytes) -> CommandResult:  # noqa: C901, PLR0911
        assert self.container_state == "running", "scripts run only in a running container"
        self.container_scripts.append(script)
        files = self.files
        if (read := _READ.match(script)) is not None:
            text = files.get(read[1])
            return result(int(read[2])) if text is None else result(stdout=text)
        if script.startswith("[ -f "):
            return result(0 if shlex.split(script)[2] in files else 1)
        if "target=" in script:
            files[_target(script)] = stdin
            return result()
        if "easyrsa init-pki" in script:
            return self._make_pki()
        if "easyrsa gen-crl" in script:
            files[CRL] = b"crl\n"
            return result()
        if (conf := _SERVER_CONF.search(script)) is not None:
            files[OPENVPN_CONFIG_PATH] = conf[1].encode()
            return result()
        if "mknod /dev/net/tun" in script or script.startswith("ifconfig eth0:0 "):
            return result()
        if script.startswith("rm -f "):
            for path in shlex.split(script)[2:]:
                files.pop(path, None)
            return result()
        if "pgrep openvpn" in script:
            return result(0 if self.openvpn_up else 1)
        raise AssertionError(f"unexpected container script: {script}")

    def _make_pki(self) -> CommandResult:
        self.pki_runs += 1
        if self.pki_fails:
            return result(1)
        self.files.update(PKI_FILES)
        return result()

    def _start_openvpn(self) -> None:
        start = self.files.get(START_SCRIPT, b"")
        self.openvpn_up = (
            self.openvpn_starts
            and b"openvpn --config" in start
            and CA_CERT in self.files
            and OPENVPN_CONFIG_PATH in self.files
        )


def _target(script: str) -> str:
    return shlex.split(script.split("target=", 1)[1].split("\n", 1)[0])[0]
