"""In-memory IPsec container answering the commands of IpsecClientManager."""

import re
import shlex
from dataclasses import dataclass, field

from amnezia_core.ipsec import (
    CA_NICKNAME,
    IKEV2_CONFIG_PATH,
    IPSEC_CA_CERT_PATH,
    IPSEC_CONTAINER,
    export_path,
)
from amnezia_core.tests.pki import FakeCa
from apps.ssh.services import CommandResult, CommandRunner, RemoteHost, RemoteProcess
from apps.ssh.tests.fakes import TIMEOUTS, FakeProcess, result

SERVER_IP = "203.0.113.7"
P12 = b"\x30\x82pkcs12"
_READ = re.compile(r"^\[ -f (\S+) \] \|\| exit (\d+); cat -- \S+$")
_EXISTS = re.compile(r"^\[ -f (\S+) \]$")
_NICKNAME = re.compile(r"-n (\S+)")
_DELETE_STATE = re.compile(r"^ipsec whack --deletestate (\d+)$")


@dataclass
class FakeIpsecServer:
    """Files of the container, the client certificates of the NSS database and pluto's state.

    `certificates` maps a nickname to its serial; `crl` is None until crlutil makes the CRL;
    `loaded_crl` is what pluto checks, read again by `ipsec crls`. `sessions` maps a client to
    its state numbers. `fail_on` makes a command with that word fail.
    """

    ca: FakeCa = field(default_factory=FakeCa)
    files: dict[str, bytes] = field(default_factory=dict)
    certificates: dict[str, int] = field(default_factory=dict)
    crl: set[int] | None = None
    loaded_crl: set[int] = field(default_factory=set)
    sessions: dict[str, list[int]] = field(default_factory=dict)
    serial: int = 0x1000
    fail_on: str = ""
    signed: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.files.setdefault(IKEV2_CONFIG_PATH, f"conn ikev2-cp\n  leftid={SERVER_IP}\n".encode())
        self.files.setdefault(IPSEC_CA_CERT_PATH, self.ca_base64.encode())

    @property
    def ca_base64(self) -> str:
        """The CA as configure_container.sh keeps it: `certutil -a` without the PEM lines."""
        return "".join(line + "\n" for line in self.ca.pem.splitlines() if "-----" not in line)

    def host(self) -> RemoteHost:
        return RemoteHost(_Runner(self), TIMEOUTS)

    def handle(self, script: str, stdin: bytes) -> CommandResult:
        words = shlex.split(script)
        assert words[:4] == ["docker", "exec", "-i", IPSEC_CONTAINER], script
        return self._in_container(words[6], stdin)

    def _in_container(self, script: str, stdin: bytes) -> CommandResult:  # noqa: PLR0911
        if self.fail_on and self.fail_on in script:
            return result(255)
        if (done := self._files(script, stdin)) is not None:
            return done
        if script.startswith("certutil -L"):
            return self._listing(script)
        if "certutil -z" in script:
            return self._sign(script)
        if "pk12util" in script:
            return self._export(script)
        if script.startswith("certutil -F"):
            self.certificates.pop(self._nickname(script))
            return result()
        if script.startswith("crlutil"):
            return self._crlutil(script, stdin.decode())
        if script.startswith("ipsec crls"):
            self.loaded_crl = set(self.crl or ())
            return result()
        return self._whack(script)

    def _files(self, script: str, stdin: bytes) -> CommandResult | None:
        if (read := _READ.match(script)) is not None:
            data = self.files.get(read[1])
            return result(int(read[2])) if data is None else result(stdout=data)
        if (exists := _EXISTS.match(script)) is not None:
            return result(0 if exists[1] in self.files else 1)
        if "target=" in script:
            target = shlex.split(script.split("target=", 1)[1].splitlines()[0])[0]
            self.files[target] = stdin
            return result()
        if script.startswith("rm -f "):
            self.files.pop(shlex.split(script)[2], None)
            return result()
        return None

    def _nickname(self, script: str) -> str:
        match = _NICKNAME.search(script)
        assert match is not None
        return shlex.split(match[1])[0]

    def _listing(self, script: str) -> CommandResult:
        if " -n " not in script:
            rows = [f"{CA_NICKNAME:<60}CTu,u,u", f"{SERVER_IP:<60}u,u,u"]
            rows += [f"{nickname:<60}u,u,u" for nickname in self.certificates]
            header = "Certificate Nickname" + " " * 40 + "Trust Attributes\n\n"
            return result(stdout=(header + "\n".join(rows) + "\n").encode())
        nickname = self._nickname(script)
        if nickname not in self.certificates:
            return result(255, stderr=b"certutil: Could not find cert\n")
        pem = self.ca.issue(nickname, self.certificates[nickname])
        return result(stdout=pem.encode())

    def _sign(self, script: str) -> CommandResult:
        assert 'certutil -z "$noise"' in script and "head -c 1024 /dev/urandom" in script
        nickname = self._nickname(script)
        assert nickname not in self.certificates, "the certificate is made twice"
        assert f"CN={nickname}" in script and "serverAuth,clientAuth" in script
        self.serial += 1
        self.certificates[nickname] = self.serial
        self.signed.append(nickname)
        return result()

    def _export(self, script: str) -> CommandResult:
        assert "-W ''" in script
        nickname = self._nickname(script)
        if nickname not in self.certificates:
            return result(255)
        self.files[export_path(nickname)] = P12 + nickname.encode()
        return result()

    def _crlutil(self, script: str, stdin: str) -> CommandResult:
        command = shlex.split(script)[1]
        if command == "-L":
            if self.crl is None:
                return result(255)
            entries = "".join(
                f"    Entry {n} (0x{n:x}):\n        Serial Number: {s} (0x{s:x})\n"
                for n, s in enumerate(sorted(self.crl), start=1)
            )
            return result(stdout=f"CRL Info:\n{entries}".encode())
        if command == "-G":
            assert self.crl is None, "the CRL is made twice"
            self.crl = set()
            return result()
        assert command == "-M" and self.crl is not None
        for line in stdin.splitlines():
            action, serial, *_rest = line.split()
            if action == "addcert":
                self.crl.add(int(serial))
            else:
                self.crl.discard(int(serial))
        return result()

    def _whack(self, script: str) -> CommandResult:
        if script == "ipsec whack --trafficstatus":
            lines = [
                f'006 #{states[0]}: "ikev2-cp"[{n}] 198.51.100.{n}, type=ESP, '
                f"id='O=IKEv2 VPN, CN={client}', lease=192.168.43.{n}/32"
                for n, (client, states) in enumerate(self.sessions.items(), start=1)
            ]
            return result(stdout="\n".join(lines).encode())
        if script == "ipsec whack --showstates":
            lines = [
                f'000 #{state}: "ikev2-cp"[{n}] 198.51.100.{n}:4500 STATE_V2_ESTABLISHED'
                for n, states in enumerate(self.sessions.values(), start=1)
                for state in states
            ]
            return result(stdout="\n".join(lines).encode())
        if (deleted := _DELETE_STATE.match(script)) is not None:
            for states in self.sessions.values():
                if int(deleted[1]) in states:
                    states.remove(int(deleted[1]))
            self.sessions = {client: states for client, states in self.sessions.items() if states}
            return result()
        raise AssertionError(f"unexpected container script: {script}")


class _Runner(CommandRunner):
    def __init__(self, server: FakeIpsecServer) -> None:
        self._server = server

    def run(
        self, command: str, *, stdin: bytes = b"", timeout: float | None = None
    ) -> CommandResult:
        if command == "id -u":
            return result(stdout=b"0\n")
        argv = shlex.split(command)
        assert argv[:2] == ["sh", "-c"], command
        return self._server.handle(argv[2], stdin)

    def start(self, command: str) -> RemoteProcess:
        return FakeProcess(["lamnezia-locked"])
