import json
import re
import shlex
from dataclasses import dataclass, field

from amnezia_core.ipsec import (
    IKEV2_CONFIG_PATH,
    IPSEC_CA_CERT_PATH,
    IPSEC_CONTAINER,
    server_identity,
)
from apps.protocols.tests.fakes import FakeDebianServer
from apps.ssh.services import CommandResult
from apps.ssh.tests.fakes import result

SCRIPT_PATH = "/opt/amnezia/configure_ipsec.sh"
CA_BASE64 = b"MIIBCA==\n"
_DOCKER_COMMANDS = {"ps", "build", "run", "inspect", "start", "restart", "exec"}
_READ = re.compile(r"^\[ -f (\S+) \] \|\| exit (\d+); cat -- \S+$")
_IKEV2_CONF = re.compile(rf"cat > {IKEV2_CONFIG_PATH} <<EOF\n(.*?)EOF\n", re.DOTALL)


@dataclass
class FakeIpsecHost(FakeDebianServer):
    """A ready Debian host running the docker commands of the IPsec installer.

    `files` is the container and `has_ca` its NSS database: both are gone with the container.
    `server_certificates` are the nicknames of the server certificates the CA signed.
    configure_container.sh starts pluto; a restart of the container stops it.
    """

    commands: set[str] = field(default_factory=lambda: {"docker", "fuser", "lsof"})
    networks: set[str] = field(default_factory=lambda: {"bridge", "amnezia-dns-net"})
    container_networks: set[str] = field(default_factory=set)
    port_users: dict[int, str] = field(default_factory=dict)
    container_state: str = ""
    files: dict[str, bytes] = field(default_factory=dict)
    host_files: dict[str, bytes] = field(default_factory=dict)
    run_arguments: list[str] = field(default_factory=list)
    has_ca: bool = False
    server_certificates: set[str] = field(default_factory=set)
    certutil_fails: bool = False
    pki_fails: bool = False
    pki_runs: int = 0
    service_runs: int = 0
    resets: int = 0
    pluto_up: bool = False
    pluto_starts: bool = True
    scripts: list[str] = field(default_factory=list)
    configure_scripts: list[str] = field(default_factory=list)

    def run(  # noqa: PLR0911
        self, command: str, *, stdin: bytes = b"", timeout: float | None = None
    ) -> CommandResult:
        argv = shlex.split(command)
        if argv[:2] != ["sh", "-c"]:
            return super().run(command, stdin=stdin, timeout=timeout)
        script = argv[2]
        words = shlex.split(script) if script.startswith("docker ") else []
        if words[:4] == ["docker", "exec", "-i", IPSEC_CONTAINER]:
            return self._in_container(words[6], stdin)
        if words[:3] == ["docker", "network", "connect"]:
            self.container_networks.add(words[3])
            return result()
        if words[1:2] and words[1] in _DOCKER_COMMANDS:
            return self._docker(words)
        if script.startswith("lsof "):
            port = int(re.search(r"':(\d+)", script)[1])  # type: ignore[index]
            return result(stdout=self.port_users.get(port, "").encode())
        if "target=" in script:
            self.host_files[_target(script)] = stdin
            return result()
        return super().run(command, stdin=stdin)

    def _docker(self, words: list[str]) -> CommandResult:
        match words[1]:
            case "ps":
                return result(stdout=f"{self.container_state}\n".encode())
            case "build":
                assert f"/opt/amnezia/{IPSEC_CONTAINER}/Dockerfile" in self.host_files
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
                self.pluto_up = False
                return result()
            case "exec":
                assert words[2] == "-d"
                return result()
        raise AssertionError(f"unexpected docker command: {words}")

    def _in_container(self, script: str, stdin: bytes) -> CommandResult:  # noqa: PLR0911
        assert self.container_state == "running", "scripts run only in a running container"
        self.scripts.append(script)
        files = self.files
        if (read := _READ.match(script)) is not None:
            text = files.get(read[1])
            return result(int(read[2])) if text is None else result(stdout=text)
        if script.startswith("[ -f "):
            return result(0 if shlex.split(script)[2] in files else 1)
        if "target=" in script:
            files[_target(script)] = stdin
            return result()
        if script.startswith("certutil -L") and "'IKEv2 VPN CA'" in script:
            return result(0 if self.has_ca else 255)
        if "certutil" in script:
            return self._certutil(script)
        if script.startswith("ipsec whack --shutdown"):
            self.resets += 1
            self.pluto_up = self.has_ca = False
            return result()
        if script.startswith(f"bash {SCRIPT_PATH}"):
            return self._configure(files.pop(SCRIPT_PATH).decode())
        if "ipsec whack --status" in script:
            return result(0 if self.pluto_up else 1)
        raise AssertionError(f"unexpected container script: {script}")

    def _certutil(self, script: str) -> CommandResult:
        nickname = shlex.split(script.split(" -n ", 1)[1])[0]
        if script.startswith("certutil -L"):
            return result(0 if nickname in self.server_certificates else 255)
        if self.certutil_fails:
            return result(255)
        if script.startswith("certutil -F"):
            self.server_certificates.remove(nickname)
            return result()
        assert self.has_ca and 'certutil -z "$noise"' in script, script
        assert f"ip:{nickname},dns:{nickname}" in script and "serverAuth" in script
        assert nickname not in self.server_certificates, "the certificate is made twice"
        self.server_certificates.add(nickname)
        return result()

    def _configure(self, script: str) -> CommandResult:
        assert "$SERVER_IP_ADDRESS" not in script and "$IPSEC_VPN_" not in script
        self.configure_scripts.append(script)
        self.pluto_up = self.pluto_starts
        if "certutil -z" not in script:
            self.service_runs += 1
            return result()
        self.pki_runs += 1
        if self.pki_fails:
            return result(1)
        conf = _IKEV2_CONF.search(script)
        assert conf is not None
        self.has_ca = True
        self.files[IKEV2_CONFIG_PATH] = conf[1].encode()
        self.server_certificates.add(server_identity(conf[1]))
        self.files[IPSEC_CA_CERT_PATH] = CA_BASE64
        return result()


def _target(script: str) -> str:
    return shlex.split(script.split("target=", 1)[1].split("\n", 1)[0])[0]
