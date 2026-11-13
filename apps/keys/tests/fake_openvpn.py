"""In-memory OpenVPN container answering the commands of OpenVpnClientManager."""

import ast
import re
import shlex

from amnezia_core.openvpn import (
    OPENVPN_CA_CERT_PATH,
    OPENVPN_CONFIG_PATH,
    OPENVPN_CONTAINER,
    OPENVPN_DIR,
    OPENVPN_INDEX_PATH,
    OPENVPN_TA_KEY_PATH,
    CertificateIndex,
    certificate_path,
    request_path,
)
from amnezia_core.tests.pki import FakeCa
from apps.keys.tests.steps import FIXTURES
from apps.ssh.services import CommandResult, CommandRunner, RemoteHost, RemoteProcess
from apps.ssh.tests.fakes import TIMEOUTS, FakeProcess, result

_READ = re.compile(r"^\[ -f (\S+) \] \|\| exit (\d+); cat -- \S+$")
_EXISTS = re.compile(r"^\[ -f (\S+) \]$")
_EASYRSA = re.compile(r"easyrsa (\S+(?: \S+)*?)(?: &&|$)")
_EXPIRES = "280210120000Z"
TA_KEY = "-----BEGIN OpenVPN Static key V1-----\nTA\n-----END OpenVPN Static key V1-----\n"
PENDING = f"{OPENVPN_DIR}/crl.pending"
CRL = f"{OPENVPN_DIR}/crl.pem"
PREVIOUS_CRL = f"{OPENVPN_DIR}/crl.pem.previous"
BROKEN_CRL = "broken"


class FakeOpenVpnServer:
    """Files of the container, an easy-rsa CA and whether openvpn runs.

    crl.pem holds the serials openvpn refuses: the R lines of pki/index.txt at the last
    gen-crl, read by openvpn only when it starts. `fail_on` makes the named easy-rsa command
    fail; `starts` decides whether openvpn comes back after a restart; with `breaks_crl` gen-crl
    writes a CRL openvpn does not start with.
    """

    def __init__(self) -> None:
        self.ca = FakeCa()
        self.files: dict[str, str] = {
            OPENVPN_CONFIG_PATH: (FIXTURES / "openvpn_server.conf").read_text(encoding="utf-8"),
            OPENVPN_CA_CERT_PATH: self.ca.pem,
            OPENVPN_TA_KEY_PATH: TA_KEY,
            OPENVPN_INDEX_PATH: "V\t360101000000Z\t\t01\tunknown\t/CN=AmneziaReq\n",
        }
        self.serial = 0x10
        self.loaded_crl: set[int] = set()
        self.running = True
        self.starts = True
        self.breaks_crl = False
        self.restarts = 0
        self.fail_on = ""
        self.easyrsa: list[str] = []

    def host(self) -> RemoteHost:
        return RemoteHost(_Runner(self), TIMEOUTS)

    def refused(self) -> set[int]:
        """Serials the running openvpn turns away."""
        return self.loaded_crl

    def handle(self, script: str, stdin: bytes) -> CommandResult:
        words = shlex.split(script)
        if words[:4] == ["docker", "exec", "-i", OPENVPN_CONTAINER]:
            return self._in_container(words[6], stdin)
        if words[:4] == ["docker", "exec", "-d", OPENVPN_CONTAINER]:
            assert not self.running, "openvpn is started twice"
            crl = self.files.get(CRL, "[]")
            self.running = self.starts and crl != BROKEN_CRL
            if self.running:
                self.loaded_crl = set(ast.literal_eval(crl))
            self.restarts += 1
            return result()
        raise AssertionError(f"unexpected host script: {script}")

    def _in_container(self, script: str, stdin: bytes) -> CommandResult:  # noqa: PLR0911
        if (read := _READ.match(script)) is not None:
            text = self.files.get(read[1])
            return result(int(read[2])) if text is None else result(stdout=text.encode())
        if (exists := _EXISTS.match(script)) is not None:
            return result(0 if exists[1] in self.files else 1)
        if "target=" in script:
            target = shlex.split(script.split("target=", 1)[1].split("\n", 1)[0])[0]
            self.files[target] = stdin.decode()
            return result()
        if "easyrsa" in script:
            return self._easyrsa(script)
        if script.startswith(("touch ", "rm -f ", "if [ -f ", "[ -f ")):
            return self._file_command(script)
        if script.startswith("killall "):
            self.running = False
            return result()
        if "pgrep openvpn" in script:
            return result(0 if self.running else 1)
        raise AssertionError(f"unexpected container script: {script}")

    def _file_command(self, script: str) -> CommandResult:
        if script.startswith("touch "):
            self.files[script.split()[1]] = ""
        elif script.startswith("rm -f "):
            for path in script.split()[2:]:
                self.files.pop(path, None)
        elif script.startswith("if [ -f ") and PREVIOUS_CRL in script:
            if CRL in self.files:
                self.files[PREVIOUS_CRL] = self.files[CRL]
            else:
                self.files.pop(PREVIOUS_CRL, None)
        elif script == f"[ -f {PREVIOUS_CRL} ] && cp -p {PREVIOUS_CRL} {CRL}":
            if PREVIOUS_CRL not in self.files:
                return result(1)
            self.files[CRL] = self.files[PREVIOUS_CRL]
        else:
            raise AssertionError(f"unexpected container script: {script}")
        return result()

    def _easyrsa(self, script: str) -> CommandResult:
        match = _EASYRSA.search(script)
        assert match is not None and "EASYRSA_BATCH=1" in script
        command, *arguments = match[1].split()
        self.easyrsa.append(match[1])
        if command == self.fail_on:
            return result(1)
        match command:
            case "import-req":
                upload, name = arguments
                assert request_path(name) not in self.files, "the request is imported twice"
                self.files[request_path(name)] = self.files[upload]
            case "sign-req":
                return self._sign(arguments[1])
            case "revoke":
                return self._revoke(arguments[0])
            case "gen-crl":
                assert "chmod 666 pki/crl.pem && cp pki/crl.pem crl.pem" in script
                lines = self.files[OPENVPN_INDEX_PATH].splitlines()
                crl = {int(line.split("\t")[3], 16) for line in lines if line[0] == "R"}
                self.files[CRL] = BROKEN_CRL if self.breaks_crl else repr(sorted(crl))
            case _:
                raise AssertionError(f"unexpected easyrsa command: {command}")
        return result()

    def _sign(self, name: str) -> CommandResult:
        assert certificate_path(name) not in self.files, "the certificate is signed twice"
        self.serial += 1
        self.files[certificate_path(name)] = self.ca.sign(
            self.files[request_path(name)], self.serial
        )
        self.files[OPENVPN_INDEX_PATH] += (
            f"V\t{_EXPIRES}\t\t{self.serial:02X}\tunknown\t/CN={name}\n"
        )
        return result()

    def _revoke(self, name: str) -> CommandResult:
        index = CertificateIndex.parse(self.files[OPENVPN_INDEX_PATH])
        if name not in index.valid_clients():
            return result(1)  # easy-rsa refuses a certificate that is not valid
        lines = self.files[OPENVPN_INDEX_PATH].splitlines(keepends=True)
        self.files[OPENVPN_INDEX_PATH] = "".join(
            line.replace("V\t", "R\t", 1).replace("\t\t", "\t261106093000Z\t", 1)
            if line.rstrip("\n").endswith(f"/CN={name}")
            else line
            for line in lines
        )
        # easy-rsa 3 moves the certificate and the request out of issued/ and reqs/.
        self.files.pop(certificate_path(name))
        self.files.pop(request_path(name), None)
        return result()


class _Runner(CommandRunner):
    def __init__(self, server: FakeOpenVpnServer) -> None:
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
