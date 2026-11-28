"""The masking check of a server: what lets a censor tell it is a VPN server, with advice.

One read-only pass over the server (addresses, listening ports, running containers, server.json
of XRay) and over what the panel knows (the keys' addresses, the AmneziaWG parameters of all
servers, its own address). The findings replace the previous report of the server. A listening
port is extra unless the panel knows something on it with the same transport (TCP or UDP), or the
administrator listed it among the server's own ports. docker-proxy of a port published on a local
address only is not public, so it is not counted. The panel checks every server once in
STEALTH_CHECK_INTERVAL on its own (`start_due`).
"""

import re
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_interface
from urllib.parse import urlsplit

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext as _

from amnezia_core.awg_fingerprint import (
    default_params,
    fixed_headers,
    is_default_signature,
    obfuscation_signature,
)
from amnezia_core.containers import AWG2, AWG_LEGACY
from amnezia_core.errors import ConfigFormatError
from amnezia_core.proxies import MTPROXY_CONTAINER, SOCKS5_CONTAINER, TELEMT_CONTAINER
from amnezia_core.xray import XRAY_CONFIG_PATH, XRAY_CONTAINER, mux_enabled
from apps.accounts.models import User
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.protocols.models import ProtocolInstance
from apps.protocols.services.installation import used_ports
from apps.protocols.services.installer import installers
from apps.relays.models import Relay
from apps.servers.models import (
    Server,
    ServerStatus,
    StealthFinding,
    StealthItem,
    StealthReport,
    StealthStatus,
    parse_own_ports,
)
from apps.ssh.errors import DockerError, RemoteFileNotFoundError, SshCommandError
from apps.ssh.services import RemoteHost

KIND = "servers.stealth"

IPAddress = IPv4Address | IPv6Address
Resolver = Callable[[str], set[IPAddress]]

_SCRIPT = (
    "echo '#addr'; ip -o addr show scope global 2>/dev/null\n"
    "echo '#listen'; ss -Hlntup 2>/dev/null\n"
    "echo '#containers'; docker ps --format '{{.Names}}' 2>/dev/null\n"
)
_WEB_PORTS = (80, 443)
_STUB_PORT = 443
_DNS_PORT = 53
_TOR_PROCESS = "tor"
_TRANSPORTS = ("tcp", "udp")
_PUBLIC_PROXIES = (SOCKS5_CONTAINER, MTPROXY_CONTAINER, TELEMT_CONTAINER)
_AWG_CONTAINERS = (AWG2.name, AWG_LEGACY.name)
_WILDCARDS = ("*", "0.0.0.0", "::")
# Columns of `ss -H` (Netid State Recv-Q Send-Q Local ...) and of `ip -o addr`.
_LOCAL_COLUMN = 4
_FAMILY_COLUMN = 2
_ADDRESS_COLUMN = 3
_PROCESS = re.compile(r'users:\(\("([^"]+)"')


@dataclass(frozen=True)
class Listener:
    """A socket listening on the server, as `ss -lntup` shows it."""

    proto: str
    host: str
    port: int
    process: str = ""

    @classmethod
    def parse(cls, line: str) -> "Listener | None":
        fields = line.split()
        if len(fields) <= _LOCAL_COLUMN or ":" not in fields[_LOCAL_COLUMN]:
            return None
        host, _sep, port = fields[_LOCAL_COLUMN].rpartition(":")
        if not port.isdigit():
            return None
        process = _PROCESS.search(line)
        host = host.strip("[]").split("%")[0]
        return cls(fields[0], host, int(port), process.group(1) if process else "")

    @property
    def public(self) -> bool:
        """Reachable from outside: on every address or on a global one, not on loopback or LAN."""
        if self.host in _WILDCARDS:
            return True
        try:
            return ip_address(self.host).is_global
        except ValueError:
            return False

    @property
    def key(self) -> tuple[int, str]:
        """(port, transport), as the panel keeps the ports it knows."""
        return self.port, self.proto

    def __str__(self) -> str:
        return f"{self.proto} {self.port}" + (f" ({self.process})" if self.process else "")


@dataclass(frozen=True)
class HostFacts:
    """What the script found on the server: global addresses, listeners, running containers."""

    addresses: tuple[IPAddress, ...]
    listeners: tuple[Listener, ...]
    containers: frozenset[str]

    @classmethod
    def parse(cls, output: str) -> "HostFacts":
        sections: dict[str, list[str]] = {}
        lines: list[str] = []
        for line in output.splitlines():
            if line.startswith("#"):
                lines = sections.setdefault(line[1:].strip(), [])
            elif line.strip():
                lines.append(line.strip())
        return cls(
            tuple(_addresses(sections.get("addr", []))),
            tuple(filter(None, map(Listener.parse, sections.get("listen", [])))),
            frozenset(sections.get("containers", [])),
        )

    @property
    def public_listeners(self) -> list[Listener]:
        return [listener for listener in self.listeners if listener.public]


class StealthCheck:
    """Looks for what gives the server away; changes nothing on it."""

    def __init__(
        self,
        server: Server,
        host: RemoteHost,
        ctx: OperationContext,
        resolve: Resolver | None = None,
    ) -> None:
        self._server = server
        self._host = host
        self._ctx = ctx
        self._resolve = resolve or resolve_host
        self._instances = list(
            ProtocolInstance.objects.filter(server=server, container_removed_at__isnull=True)
        )

    def run(self) -> list[StealthFinding]:
        with self._ctx.step(_("Read the addresses, ports and containers of the server")):
            facts = HostFacts.parse(self._host.sh(_SCRIPT).output)
            self._ctx.log(
                _("%(addresses)d global addresses, %(ports)d public ports")
                % {"addresses": len(facts.addresses), "ports": len(facts.public_listeners)}
            )
        with self._ctx.step(_("Compare with the panel and the other servers")):
            findings = self._logged(
                self._ipv6(facts),
                *self._ports(facts),
                self._proxies(facts),
                self._panel(facts),
                *self._awg(),
            )
        with self._ctx.step(_("Read the XRay config")):
            findings += self._logged(self._mux())
        return findings

    def _logged(self, *findings: StealthFinding) -> list[StealthFinding]:
        for finding in findings:
            if finding.warns:
                self._ctx.log(finding.problem)
        return list(findings)

    def _ipv6(self, facts: HostFacts) -> StealthFinding:
        own = [address for address in facts.addresses if isinstance(address, IPv6Address)]
        if not own:
            return _ok(StealthItem.IPV6)
        hosts = [self._server.host, *self._server.addresses.values_list("host", flat=True)]
        used = set().union(*(self._resolve(host) for host in hosts))
        unused = [str(address) for address in own if address not in used]
        return _verdict(StealthItem.IPV6, [] if len(unused) < len(own) else unused)

    def _ports(self, facts: HostFacts) -> list[StealthFinding]:
        listeners = facts.public_listeners
        if not listeners:  # sshd always listens: ss is missing
            return [_skip(StealthItem.PORTS), _skip(StealthItem.STUB)]
        expected = self._expected_ports()
        extra = sorted({str(item) for item in listeners if item.key not in expected})
        stub = any(item.proto == "tcp" and item.port == _STUB_PORT for item in listeners)
        return [_verdict(StealthItem.PORTS, extra), _flag(StealthItem.STUB, not stub)]

    def _expected_ports(self) -> set[tuple[int, str]]:
        """(port, transport) of what the panel knows on the server and of its own ports."""
        server = self._server
        ports = used_ports(server) | parse_own_ports(server.own_ports)
        ports.update((port, "tcp") for port in (server.port, *_WEB_PORTS))
        for service in server.services.all():
            if service.port is None:
                continue
            installer = installers.find(service.container)
            if installer:
                ports |= installer.ports_of(service.port, {})
            else:
                ports.update((service.port, transport) for transport in _TRANSPORTS)
        ports.update((port, "udp") for port in server.relayed_groups.values_list("port", flat=True))
        if Relay.objects.filter(server=server).exists():
            ports.update((_DNS_PORT, transport) for transport in _TRANSPORTS)
        return ports

    def _proxies(self, facts: HostFacts) -> StealthFinding:
        found = [name for name in _PUBLIC_PROXIES if name in facts.containers]
        if any(item.process == _TOR_PROCESS for item in facts.public_listeners):
            found.append(_("Tor node"))
        return _verdict(StealthItem.PROXY, found)

    def _panel(self, facts: HostFacts) -> StealthFinding:
        panel = self._resolve(urlsplit(settings.SITE_URL).hostname or "")
        if not panel:
            return _skip(StealthItem.PANEL)
        shared = panel & (set(facts.addresses) | self._resolve(self._server.host))
        return _verdict(StealthItem.PANEL, sorted(map(str, shared)))

    def _awg(self) -> list[StealthFinding]:
        own = [item for item in self._instances if item.container in _AWG_CONTAINERS]
        if not own:
            items = (StealthItem.AWG_DEFAULTS, StealthItem.AWG_SHARED, StealthItem.AWG_RANGES)
            return [_skip(item) for item in items]
        defaults = _unique(name for item in own for name in default_params(item.params))
        return [_verdict(StealthItem.AWG_DEFAULTS, defaults), self._shared(own), _ranges(own)]

    def _shared(self, own: list[ProtocolInstance]) -> StealthFinding:
        """Other servers with the same values; defaults are told apart, members of a group share."""
        others = (
            ProtocolInstance.objects.filter(
                container__in=_AWG_CONTAINERS, container_removed_at__isnull=True
            )
            .exclude(server=self._server)
            .select_related("server", "group_member")
        )
        names: list[str] = []
        for instance in own:
            signature = obfuscation_signature(instance.params)
            if is_default_signature(signature):
                continue
            group = _group_of(instance)
            names.extend(
                other.server.name
                for other in others
                if obfuscation_signature(other.params) == signature
                and (group is None or _group_of(other) != group)
            )
        return _verdict(StealthItem.AWG_SHARED, _unique(names))

    def _mux(self) -> StealthFinding:
        if not any(instance.container == XRAY_CONTAINER for instance in self._instances):
            return _skip(StealthItem.XRAY_MUX)
        try:
            config = self._host.container(XRAY_CONTAINER).read_file(XRAY_CONFIG_PATH).decode()
            enabled = mux_enabled(config)
        except (RemoteFileNotFoundError, SshCommandError, DockerError, ConfigFormatError) as error:
            self._ctx.log(str(error))
            return _skip(StealthItem.XRAY_MUX)
        return _flag(StealthItem.XRAY_MUX, enabled)


def resolve_host(host: str) -> set[IPAddress]:
    """The addresses of a host name or IP; empty when it does not resolve."""
    if not host:
        return set()
    try:
        answers = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return set()
    return {ip_address(str(answer[4][0]).split("%")[0]) for answer in answers}


def save(server: Server, findings: list[StealthFinding]) -> StealthReport:
    report, _created = StealthReport.objects.update_or_create(
        server=server,
        defaults={
            "results": [finding.as_json() for finding in findings],
            "checked_at": timezone.now(),
        },
    )
    return report


def start(server: Server, user: User | None) -> Operation:
    return launch(KIND, {"server_id": server.pk}, user=user, subject=server.name)


def start_due(now: datetime) -> list[Operation]:
    """Checks of the ready servers that answer, not checked in STEALTH_CHECK_INTERVAL.

    A server with a check waiting or under way is left to it.
    """
    if not settings.STEALTH_CHECK_INTERVAL:
        return []
    fresh = now - timedelta(seconds=settings.STEALTH_CHECK_INTERVAL)
    busy = Operation.objects.filter(
        kind=KIND, status__in=(OperationStatus.PENDING, OperationStatus.RUNNING)
    ).values_list("params__server_id", flat=True)
    servers = (
        Server.objects.filter(status=ServerStatus.READY)
        .exclude(stealth_report__checked_at__gte=fresh)
        .exclude(pk__in=list(busy))
        .select_related("latest_sample")
    )
    return [start(server, None) for server in servers if server.online]


def _addresses(lines: list[str]) -> Iterable[IPAddress]:
    for line in lines:
        fields = line.split()
        if len(fields) > _ADDRESS_COLUMN and fields[_FAMILY_COLUMN] in ("inet", "inet6"):
            try:
                yield ip_interface(fields[_ADDRESS_COLUMN]).ip
            except ValueError:
                continue


def _ranges(own: list[ProtocolInstance]) -> StealthFinding:
    """H1-H4 fixed on AmneziaWG 2.0; the older container takes no ranges."""
    awg2 = [item for item in own if item.container == AWG2.name]
    if not awg2:
        return _skip(StealthItem.AWG_RANGES)
    fixed = _unique(name for item in awg2 for name in fixed_headers(item.params))
    return _verdict(StealthItem.AWG_RANGES, fixed)


def _group_of(instance: ProtocolInstance) -> int | None:
    member = getattr(instance, "group_member", None)
    return member.group_id if member else None


def _unique(names: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(names))


def _ok(item: StealthItem) -> StealthFinding:
    return StealthFinding(item, StealthStatus.OK)


def _skip(item: StealthItem) -> StealthFinding:
    return StealthFinding(item, StealthStatus.SKIP)


def _flag(item: StealthItem, found: bool) -> StealthFinding:
    return StealthFinding(item, StealthStatus.WARN if found else StealthStatus.OK)


def _verdict(item: StealthItem, facts: list[str]) -> StealthFinding:
    """A warning about `facts`; none found is fine."""
    if not facts:
        return _ok(item)
    return StealthFinding(item, StealthStatus.WARN, ", ".join(facts))
