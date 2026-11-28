"""The Reality SNI from the neighbours: the scan, its result, the warnings and the settings page."""

import shlex
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from ipaddress import IPv4Network
from typing import Any

import pytest
from django.test import Client

from amnezia_core.xray import DEFAULT_SITE, XRAY_CONTAINER
from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.context import OperationContext
from apps.operations.tests.factories import OperationFactory
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.models import InstanceOrigin, ProtocolInstance, SniScan
from apps.protocols.services import sni_scan
from apps.protocols.services.sni_choice import SniAdvice, is_mass_sni
from apps.protocols.services.sni_scan import (
    SniCandidate,
    SniScanner,
    network_of,
    parse_scan,
    scan_script,
)
from apps.protocols.services.xray_settings import XRAY_SETTINGS_KIND
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import CommandResult, CommandRunner, RemoteHost, RemoteProcess
from apps.ssh.tests.fakes import TIMEOUTS, FakeProcess, result

pytestmark = pytest.mark.django_db

type OnCommit = Callable[..., Any]

SERVER_IP = "203.0.113.10"

OUTPUT = """\
@@ 203.0.113.7
ALPN protocol: h2
subject=CN = shop.example.net
X509v3 Subject Alternative Name:
    DNS:shop.example.net, DNS:www.shop.example.net
@@ 203.0.113.8
ALPN protocol: http/1.1
subject=CN = old.example.org
@@ 203.0.113.9
ALPN protocol: h2
subject=CN = *.cdn.example.com
X509v3 Subject Alternative Name:
    DNS:*.cdn.example.com, DNS:cdn.example.com
@@ 203.0.113.11
ALPN protocol: h2
subject=CN = 203.0.113.11
@@ 203.0.113.12
ALPN protocol: h2
subject=O = Example, CN = shop.example.net
"""


@dataclass
class FakeScanHost(CommandRunner):
    """A host that answers the scripts of sni_scan.py: openssl, apt and the scan itself."""

    openssl: bool = True
    apt_fails: bool = False
    scan_fails: bool = False
    output: str = OUTPUT
    scripts: list[str] = field(default_factory=list)
    installed: list[str] = field(default_factory=list)
    locks: int = 0

    def host(self) -> RemoteHost:
        return RemoteHost(self, TIMEOUTS)

    def run(
        self, command: str, *, stdin: bytes = b"", timeout: float | None = None
    ) -> CommandResult:
        if command == "id -u":
            return result(stdout=b"0\n")
        argv = shlex.split(command)
        assert argv[:2] == ["sh", "-c"], command
        script = argv[2]
        self.scripts.append(script)
        if script == sni_scan.HAS_OPENSSL:
            return result(0 if self.openssl else 1)
        if "apt-get" in script:
            if self.apt_fails:
                return result(100, stderr=b"E: Unable to locate package")
            self.installed.append("openssl")
            self.openssl = True
            return result()
        if "s_client" in script:
            return result(1) if self.scan_fails else result(stdout=self.output.encode())
        raise AssertionError(f"unexpected script: {script}")

    def start(self, command: str) -> RemoteProcess:
        self.locks += 1
        return FakeProcess(["lamnezia-locked"])


@pytest.fixture
def server() -> Server:
    return ServerFactory(name="Frankfurt", host=SERVER_IP)


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> FakeScanHost:
    fake = FakeScanHost()

    @contextmanager
    def connect(_server: Server) -> Iterator[RemoteHost]:
        yield fake.host()

    monkeypatch.setattr("apps.protocols.operations.connect", connect)
    return fake


@pytest.fixture
def admin_client(client: Client) -> Client:
    client.force_login(UserFactory(role=Role.ADMIN))
    return client


def xray_instance(server: Server, sni: str = DEFAULT_SITE) -> ProtocolInstance:
    return ProtocolInstance.objects.create(
        server=server,
        container=XRAY_CONTAINER,
        port=443,
        subnet="",
        params={"site": sni, "sni": sni, "flow": "xtls-rprx-vision", "fingerprint": "chrome"},
        public_key="pub",
        origin=InstanceOrigin.INSTALLED,
    )


def scanner(server: Server, host: FakeScanHost) -> tuple[SniScanner, Operation]:
    operation = OperationFactory()
    return SniScanner(server, host.host(), OperationContext(operation)), operation


def log_of(operation: Operation) -> str:
    return "\n".join(operation.steps.values_list("log", flat=True))


# The script and its output


def test_network_is_the_slash_24_of_the_server() -> None:
    assert network_of(SERVER_IP) == IPv4Network("203.0.113.0/24")


def test_script_probes_the_neighbours_slowly_and_skips_the_server() -> None:
    script = scan_script(network_of(SERVER_IP), SERVER_IP)

    assert "203.0.113.1\n" in script
    assert "203.0.113.254" in script
    assert f"{SERVER_IP}\n" not in script
    assert "203.0.113.0\n" not in script
    assert f"xargs -P {sni_scan.WORKERS}" in script
    assert f"sleep {sni_scan.PAUSE_SECONDS}" in script
    assert f"timeout {sni_scan.CONNECT_SECONDS} openssl s_client" in script
    assert "-tls1_3 -alpn h2" in script


def test_parse_keeps_h2_neighbours_with_a_domain_once_each() -> None:
    assert parse_scan(OUTPUT) == [
        SniCandidate("203.0.113.9", "cdn.example.com"),
        SniCandidate("203.0.113.7", "shop.example.net"),
    ]


def test_parse_of_nothing_is_empty() -> None:
    assert parse_scan("") == []


# The scan on the server


def test_scan_keeps_the_domains(server: Server, host: FakeScanHost) -> None:
    sni, operation = scanner(server, host)

    scan = sni.scan(SERVER_IP)

    assert host.locks == 1
    assert host.installed == []
    assert scan.network == "203.0.113.0/24"
    assert scan.candidates == [
        {"ip": "203.0.113.9", "domain": "cdn.example.com"},
        {"ip": "203.0.113.7", "domain": "shop.example.net"},
    ]
    assert "shop.example.net (203.0.113.7)" in log_of(operation)
    assert sni_scan.candidates_of(server)[1] == SniCandidate("203.0.113.7", "shop.example.net")


def test_scan_installs_openssl_when_missing(server: Server) -> None:
    host = FakeScanHost(openssl=False)

    scanner(server, host)[0].scan(SERVER_IP)

    assert host.installed == ["openssl"]


def test_failed_openssl_install_is_reported(server: Server) -> None:
    host = FakeScanHost(openssl=False, apt_fails=True)

    with pytest.raises(SetupError) as caught:
        scanner(server, host)[0].scan(SERVER_IP)

    assert caught.value.code == SetupErrorCode.SNI_SCAN_TOOLS_FAILED
    assert not SniScan.objects.exists()


def test_failed_scan_keeps_the_previous_result(server: Server) -> None:
    sni_scan.record(server, network_of(SERVER_IP), [SniCandidate("203.0.113.7", "a.example")])
    host = FakeScanHost(scan_fails=True)

    with pytest.raises(SetupError) as caught:
        scanner(server, host)[0].scan(SERVER_IP)

    assert caught.value.code == SetupErrorCode.SNI_SCAN_FAILED
    assert sni_scan.candidates_of(server) == [SniCandidate("203.0.113.7", "a.example")]


# The warnings


def test_default_site_is_a_widely_used_sni() -> None:
    assert is_mass_sni(DEFAULT_SITE)
    assert is_mass_sni(" WWW.Microsoft.com ")
    assert not is_mass_sni("shop.example.net")


def test_advice_warns_of_an_sni_of_another_server(server: Server) -> None:
    instance = xray_instance(server, "shop.example.net")
    xray_instance(ServerFactory(name="Helsinki"), "shop.example.net")
    xray_instance(ServerFactory(name="Amsterdam"), "Shop.Example.net")
    xray_instance(ServerFactory(name="Riga"), DEFAULT_SITE)
    sni_scan.record(server, network_of(SERVER_IP), parse_scan(OUTPUT))

    advice = SniAdvice(instance)

    assert not advice.mass
    assert advice.shared_with == ["Amsterdam", "Helsinki"]
    assert advice.domains == ["cdn.example.com", "shop.example.net"]
    assert [option.shared_with for option in advice.options] == [[], ["Amsterdam", "Helsinki"]]


def test_advice_warns_of_the_default_sni(server: Server) -> None:
    advice = SniAdvice(xray_instance(server))

    assert advice.mass
    assert advice.shared_with == []
    assert advice.scan is None


# The settings page and its buttons


def test_settings_page_shows_the_warnings_and_the_domains(
    admin_client: Client, server: Server
) -> None:
    instance = xray_instance(server)
    xray_instance(ServerFactory(name="Helsinki"))
    sni_scan.record(server, network_of(SERVER_IP), parse_scan(OUTPUT))

    page = admin_client.get(f"/protocols/{instance.pk}/settings/").content.decode()

    assert "SNI from the neighbours of the server" in page
    assert "is widely used" in page
    assert "is also used by XRay on: Helsinki" in page
    assert "the hoster may take it for a port scan" in page
    assert 'value="shop.example.net"' in page
    assert "Apply the domain" in page


def test_scan_button_starts_the_operation(
    django_capture_on_commit_callbacks: OnCommit,
    admin_client: Client,
    server: Server,
    host: FakeScanHost,
) -> None:
    instance = xray_instance(server)

    with django_capture_on_commit_callbacks(execute=True):
        response = admin_client.post(f"/protocols/{instance.pk}/sni-scan/")

    operation = Operation.objects.get()
    assert response.status_code == 302
    assert operation.kind == sni_scan.SCAN_KIND
    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    assert AuditEvent.objects.get().action == sni_scan.SCAN_KIND
    assert sni_scan.candidates_of(server)[0].domain == "cdn.example.com"


def test_apply_starts_the_settings_change_with_the_domain(
    monkeypatch: pytest.MonkeyPatch, admin_client: Client, server: Server
) -> None:
    instance = xray_instance(server)
    sni_scan.record(server, network_of(SERVER_IP), parse_scan(OUTPUT))
    started: list[Any] = []

    def start(instance: ProtocolInstance, settings: Any, user: User, *, reissue: bool) -> Any:
        started.append((instance, settings, reissue))
        return OperationFactory(kind=XRAY_SETTINGS_KIND)

    monkeypatch.setattr("apps.protocols.settings_forms.start_settings_change", start)

    response = admin_client.post(
        f"/protocols/{instance.pk}/sni-apply/",
        {"domain": "shop.example.net", "confirm": "on"},
    )

    assert response.status_code == 302
    [(target, settings, reissue)] = started
    assert target == instance
    assert (settings.server.site, settings.server.sni) == ("shop.example.net", "shop.example.net")
    assert settings.server.port == 443
    assert settings.server.fingerprint == "chrome"
    assert not reissue


@pytest.mark.parametrize(
    "data",
    [{"domain": "evil.example", "confirm": "on"}, {"domain": "shop.example.net"}],
    ids=["unknown domain", "no confirmation"],
)
def test_apply_refuses_a_bad_choice(
    admin_client: Client, server: Server, data: dict[str, str]
) -> None:
    instance = xray_instance(server)
    sni_scan.record(server, network_of(SERVER_IP), parse_scan(OUTPUT))

    response = admin_client.post(f"/protocols/{instance.pk}/sni-apply/", data)

    assert response.status_code == 302
    assert response["Location"] == f"/protocols/{instance.pk}/settings/"
    assert not Operation.objects.exists()


def test_clients_cannot_scan(client: Client, server: Server) -> None:
    instance = xray_instance(server)
    client.force_login(UserFactory())

    response = client.post(f"/protocols/{instance.pk}/sni-scan/")

    assert response.status_code in {302, 403, 404}
    assert not Operation.objects.exists()
