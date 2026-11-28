"""Scan of the /24 of a server for a Reality SNI: neighbours that answer TLS 1.3 with h2.

A widely used SNI is easy to tell apart from the network of the server, so the domain of a
neighbour fits Reality better, as RealiTLScanner (XTLS) finds it. The server itself connects to
port 443 of each address of its /24 with openssl, a few at a time and with a pause, and reads the
names of the certificate. Only that /24 is touched; the hoster may still take it for a port scan.
The latest result is kept in `SniScan`, so the settings page shows it without connecting.
"""

import re
import shlex
from collections.abc import Iterable
from contextlib import ExitStack
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from typing import Any, Self

from django.utils import timezone
from django.utils.translation import gettext as _

from amnezia_core.xray import is_valid_site
from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.models import SniScan
from apps.protocols.services import host_scripts
from apps.protocols.services.host_setup import log_tail
from apps.servers.models import Server
from apps.ssh.services import RemoteHost

SCAN_KIND = "protocols.scan_sni"
PREFIX_LENGTH = 24

# The rate limit: so many connections at a time, each after a pause, each cut off after a while.
WORKERS = 4
PAUSE_SECONDS = 0.5
CONNECT_SECONDS = 3
# The whole /24 at the slowest: every address waits for its pause and its cut-off.
SCAN_TIMEOUT = 600

HAS_OPENSSL = "command -v openssl >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1"
_BLOCK = "@@ "
_ALPN_H2 = "ALPN protocol: h2"
_COMMON_NAME = re.compile(r"\bCN\s*=\s*([^,/\s]+)")
_DNS_NAME = re.compile(r"\bDNS:([^,\s]+)")


@dataclass(frozen=True)
class SniCandidate:
    """A neighbour that answers TLS 1.3 with h2, and the domain of its certificate."""

    ip: str
    domain: str

    def to_json(self) -> dict[str, str]:
        return {"ip": self.ip, "domain": self.domain}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Self:
        return cls(str(data["ip"]), str(data["domain"]))


def network_of(server_ip: str) -> IPv4Network:
    """The /24 of the server address."""
    return IPv4Network(f"{server_ip}/{PREFIX_LENGTH}", strict=False)


def scan_script(network: IPv4Network, server_ip: str) -> str:
    """The script that probes each host of `network` but the server and prints one block per
    neighbour that finished a TLS 1.3 handshake: `@@ <ip>`, the ALPN line, the certificate names.
    """
    own = IPv4Address(server_ip)
    hosts = "\n".join(str(host) for host in network.hosts() if host != own)
    probe = (
        f"sleep {PAUSE_SECONDS}\n"
        f'out=$(timeout {CONNECT_SECONDS} openssl s_client -connect "$1:443" -tls1_3 -alpn h2'
        " </dev/null 2>/dev/null) || exit 0\n"
        "{\n"
        f'  printf "%s\\n" "$out" | grep -m1 "^ALPN protocol:"\n'
        '  printf "%s\\n" "$out" | openssl x509 -noout -subject -ext subjectAltName 2>/dev/null\n'
        '} >"$2/$1"\n'
    )
    return (
        "dir=$(mktemp -d) || exit 1\n"
        "trap 'rm -rf \"$dir\"' EXIT\n"
        f"printf '%s\\n' {shlex.quote(hosts)} |\n"
        f'  xargs -P {WORKERS} -I @ sh -c {shlex.quote(probe)} _ @ "$dir"\n'
        'for file in "$dir"/*; do\n'
        '  [ -f "$file" ] || continue\n'
        f'  echo "{_BLOCK}${{file##*/}}"\n'
        '  cat "$file"\n'
        "done\n"
    )


def parse_scan(output: str) -> list[SniCandidate]:
    """The neighbours with h2 and a usable domain, one per domain, sorted by it."""
    found: dict[str, SniCandidate] = {}
    for ip, lines in _blocks(output):
        domain = _domain(lines)
        if _ALPN_H2 in lines and domain and domain not in found:
            found[domain] = SniCandidate(ip, domain)
    return [found[domain] for domain in sorted(found)]


def _blocks(output: str) -> Iterable[tuple[str, list[str]]]:
    ip = ""
    lines: list[str] = []
    for line in output.splitlines():
        if line.startswith(_BLOCK):
            if ip:
                yield ip, lines
            ip, lines = line.removeprefix(_BLOCK).strip(), []
        elif ip:
            lines.append(line.strip())
    if ip:
        yield ip, lines


def _domain(lines: list[str]) -> str | None:
    """The common name of the certificate, else its first alternative name: not a wildcard and
    not an address, so that it can be the masking site."""
    text = "\n".join(lines)
    subject = next((line for line in lines if line.startswith("subject")), "")
    names = _COMMON_NAME.findall(subject) + _DNS_NAME.findall(text)
    return next((name.lower() for name in names if _usable(name)), None)


def _usable(name: str) -> bool:
    return "*" not in name and not name.replace(".", "").isdigit() and is_valid_site(name)


class SniScanner:
    """Scans the /24 of a server from the server itself and keeps the result."""

    def __init__(self, server: Server, host: RemoteHost, ctx: OperationContext) -> None:
        self._server = server
        self._host = host
        self._ctx = ctx

    def scan(self, server_ip: str) -> SniScan:
        network = network_of(server_ip)
        with ExitStack() as stack:
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(self._host.lock())
            with self._ctx.step(_("Install openssl")):
                self._install_tools()
        with self._ctx.step(_("Scan %(network)s") % {"network": network}):
            result = self._host.sh(scan_script(network, server_ip), timeout=SCAN_TIMEOUT)
            if not result.ok:
                log_tail(self._ctx, result)
                raise SetupError(SetupErrorCode.SNI_SCAN_FAILED, f"exit {result.exit_code}")
            candidates = parse_scan(result.output)
            self._ctx.log(_("Domains with TLS 1.3 and h2: %(count)d.") % {"count": len(candidates)})
            for candidate in candidates:
                self._ctx.log(f"{candidate.domain} ({candidate.ip})")
        return record(self._server, network, candidates)

    def _install_tools(self) -> None:
        if self._host.sh(HAS_OPENSSL).ok:
            self._ctx.log(_("Everything is already installed."))
            return
        result = self._host.sh(
            host_scripts.install_packages(["openssl"]), timeout=host_scripts.APT_TIMEOUT
        )
        if not result.ok:
            log_tail(self._ctx, result)
            raise SetupError(SetupErrorCode.SNI_SCAN_TOOLS_FAILED, f"exit {result.exit_code}")


def record(server: Server, network: IPv4Network, candidates: list[SniCandidate]) -> SniScan:
    scan, _created = SniScan.objects.update_or_create(
        server=server,
        defaults={
            "network": str(network),
            "candidates": [candidate.to_json() for candidate in candidates],
            "scanned_at": timezone.now(),
        },
    )
    return scan


def candidates_of(server: Server) -> list[SniCandidate]:
    """The domains of the latest scan of the server, if any."""
    scan = SniScan.objects.filter(server=server).first()
    return [SniCandidate.from_json(item) for item in scan.candidates] if scan else []


def start(server: Server, user: User, *, ip: str | None = None) -> Operation:
    """Start a scan; it goes to the audit journal, since it touches the neighbours."""
    operation = launch(SCAN_KIND, {"server_id": server.pk}, user=user, subject=server.name)
    audit.log(SCAN_KIND, actor=user, target=server, data={"operation": operation.pk}, ip=ip)
    return operation
