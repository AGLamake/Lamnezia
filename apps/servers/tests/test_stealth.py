from ipaddress import ip_address
from typing import Any

import pytest
from django.test import Client

from amnezia_core.keys import KeyPair
from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.backups.tests.helpers import awg_instance
from apps.groups.models import GroupMember, ServerGroup
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.tests.factories import OperationFactory
from apps.protocols.models import InstanceOrigin, ProtocolInstance
from apps.servers.models import (
    Server,
    StealthFinding,
    StealthItem,
    StealthReport,
    StealthStatus,
)
from apps.servers.services import stealth
from apps.servers.services.stealth import HostFacts, Listener, StealthCheck
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import RemoteHost
from apps.ssh.tests.fakes import TIMEOUTS, FakeRunner, result

pytestmark = pytest.mark.django_db

SERVER_IP = "91.198.174.10"
PANEL_IP = "198.51.100.7"
OWN_PARAMS = {
    "Jc": "5", "Jmin": "20", "Jmax": "70", "S1": "31", "S2": "47", "S3": "18", "S4": "25",
    "H1": "100-200", "H2": "300-400", "H3": "500-600", "H4": "700-800",
}  # fmt: skip
HOST_OUTPUT = f"""#addr
2: eth0    inet {SERVER_IP}/24 brd 91.198.174.255 scope global eth0\\
2: eth0    inet6 2001:db8::10/64 scope global \\
#listen
tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=1,fd=3))
tcp LISTEN 0 128 [::]:22 [::]:* users:(("sshd",pid=1,fd=4))
tcp LISTEN 0 4096 127.0.0.1:5432 0.0.0.0:* users:(("postgres",pid=2,fd=5))
udp UNCONN 0 0 0.0.0.0:40001 0.0.0.0:* users:(("docker-proxy",pid=3,fd=4))
tcp LISTEN 0 4096 0.0.0.0:8080 0.0.0.0:* users:(("nginx",pid=4,fd=6))
tcp LISTEN 0 4096 {SERVER_IP}:9001 0.0.0.0:* users:(("tor",pid=5,fd=7))
#containers
amnezia-awg2
amnezia-socks5proxy
"""


def resolver(table: dict[str, list[str]]) -> stealth.Resolver:
    return lambda host: {ip_address(ip) for ip in table.get(host, [])}


def run(
    server: Server,
    output: str = HOST_OUTPUT,
    xray: bytes | None = None,
    table: dict[str, list[str]] | None = None,
) -> dict[StealthItem, StealthFinding]:
    answers = {"server.json": result(stdout=xray)} if xray is not None else {}
    runner = FakeRunner({**answers, "#addr": result(stdout=output.encode())})
    resolve = resolver(table or {SERVER_IP: [SERVER_IP], "panel.example": [PANEL_IP]})
    check = StealthCheck(
        server, RemoteHost(runner, TIMEOUTS), OperationContext(OperationFactory()), resolve
    )
    return {finding.item: finding for finding in check.run()}


@pytest.fixture(autouse=True)
def panel(settings: Any) -> None:
    settings.SITE_URL = "https://panel.example"


@pytest.fixture
def server() -> Server:
    return ServerFactory(host=SERVER_IP)


def xray_instance(server: Server) -> ProtocolInstance:
    keys = KeyPair.generate()
    return ProtocolInstance.objects.create(
        server=server,
        container="amnezia-xray",
        port=443,
        subnet="",
        public_key=keys.public_key,
        origin=InstanceOrigin.INSTALLED,
    )


def test_listener_lines_of_ss_are_read() -> None:
    line = 'udp UNCONN 0 0 127.0.0.53%lo:53 0.0.0.0:* users:(("systemd-resolve",pid=9,fd=13))'

    listener = Listener.parse(line)

    assert listener == Listener("udp", "127.0.0.53", 53, "systemd-resolve")
    assert listener is not None and not listener.public
    assert Listener.parse("tcp LISTEN 0 128 *:443 *:*") == Listener("tcp", "*", 443)
    assert Listener.parse("garbage") is None


def test_host_facts_keep_global_addresses_and_containers() -> None:
    facts = HostFacts.parse(HOST_OUTPUT)

    assert [str(address) for address in facts.addresses] == [SERVER_IP, "2001:db8::10"]
    assert {listener.port for listener in facts.public_listeners} == {22, 40001, 8080, 9001}
    assert facts.containers == {"amnezia-awg2", "amnezia-socks5proxy"}


def test_a_server_set_up_with_the_app_defaults_gets_warnings_with_advice(server: Server) -> None:
    awg_instance(server)

    found = run(server)

    status = {item: finding.status for item, finding in found.items()}
    assert status == {
        StealthItem.IPV6: StealthStatus.WARN,
        StealthItem.PORTS: StealthStatus.WARN,
        StealthItem.STUB: StealthStatus.WARN,
        StealthItem.PROXY: StealthStatus.WARN,
        StealthItem.PANEL: StealthStatus.OK,
        StealthItem.AWG_DEFAULTS: StealthStatus.WARN,
        StealthItem.AWG_SHARED: StealthStatus.OK,
        StealthItem.AWG_RANGES: StealthStatus.WARN,
        StealthItem.XRAY_MUX: StealthStatus.SKIP,
    }
    assert found[StealthItem.IPV6].facts == "2001:db8::10"
    assert found[StealthItem.PORTS].facts == "tcp 8080 (nginx), tcp 9001 (tor)"
    assert found[StealthItem.PROXY].facts == "amnezia-socks5proxy, Tor node"
    assert found[StealthItem.AWG_RANGES].facts == "H1, H2, H3, H4"
    assert "H1" in found[StealthItem.AWG_DEFAULTS].problem
    assert all(finding.advice for finding in found.values())


def test_a_tidy_server_passes(server: Server) -> None:
    instance = awg_instance(server)
    instance.params = OWN_PARAMS
    instance.save()
    xray_instance(server)
    output = (
        f"#addr\n2: eth0 inet {SERVER_IP}/24 scope global eth0\n#listen\n"
        'tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=1,fd=3))\n'
        "udp UNCONN 0 0 0.0.0.0:40001 0.0.0.0:*\n"
        "tcp LISTEN 0 4096 0.0.0.0:443 0.0.0.0:*\n#containers\namnezia-awg2\namnezia-xray\n"
    )

    found = run(server, output, xray=b'{"inbounds": [{"port": 443}]}')

    assert {finding.status for finding in found.values()} == {StealthStatus.OK}


def test_the_panel_on_the_server_address_and_mux_are_reported(server: Server) -> None:
    xray_instance(server)

    found = run(
        server,
        xray=b'{"outbounds": [{"mux": {"enabled": true}}]}',
        table={SERVER_IP: [SERVER_IP], "panel.example": [SERVER_IP]},
    )

    assert found[StealthItem.PANEL].status == StealthStatus.WARN
    assert found[StealthItem.PANEL].facts == SERVER_IP
    assert found[StealthItem.XRAY_MUX].status == StealthStatus.WARN
    assert found[StealthItem.AWG_DEFAULTS].status == StealthStatus.SKIP


def test_ipv6_used_by_the_keys_is_fine(server: Server) -> None:
    server.addresses.create(host="v6.example")

    found = run(server, table={SERVER_IP: [SERVER_IP], "v6.example": ["2001:db8::10"]})

    assert found[StealthItem.IPV6].status == StealthStatus.OK
    assert found[StealthItem.PANEL].status == StealthStatus.SKIP


def test_parameters_copied_from_another_server_are_reported_but_not_in_a_group(
    server: Server,
) -> None:
    own, other = awg_instance(server), awg_instance(ServerFactory(name="Frankfurt"))
    for instance in (own, other):
        instance.params = OWN_PARAMS
        instance.save()

    assert run(server)[StealthItem.AWG_SHARED].facts == "Frankfurt"

    group = ServerGroup.objects.create(name="Europe", port=40001)
    GroupMember.objects.create(group=group, instance=own, priority=0)
    GroupMember.objects.create(group=group, instance=other, priority=1)

    assert run(server)[StealthItem.AWG_SHARED].status == StealthStatus.OK


def test_ports_of_the_panel_are_expected(server: Server) -> None:
    awg_instance(server)
    server.probe_port = 8080
    server.save()
    ServerGroup.objects.create(name="Asia", port=9001, relay=server)

    found = run(server)

    assert found[StealthItem.PORTS].status == StealthStatus.OK


def test_without_ss_the_ports_are_not_checked(server: Server) -> None:
    found = run(server, output="#addr\n#listen\n#containers\n")

    assert found[StealthItem.PORTS].status == StealthStatus.SKIP
    assert found[StealthItem.STUB].status == StealthStatus.SKIP


def test_the_report_replaces_the_previous_one(server: Server) -> None:
    stealth.save(server, [StealthFinding(StealthItem.STUB, StealthStatus.WARN)])
    report = stealth.save(server, [StealthFinding(StealthItem.STUB, StealthStatus.OK)])

    assert StealthReport.objects.get() == report
    assert report.findings == [StealthFinding(StealthItem.STUB, StealthStatus.OK)]
    assert report.warnings == 0


@pytest.fixture
def admin_client(client: Client) -> Client:
    client.force_login(UserFactory(role=Role.ADMIN))
    return client


def test_the_check_starts_from_the_server_card(admin_client: Client, server: Server) -> None:
    html = admin_client.get(f"/servers/{server.pk}/").content.decode()
    assert f'action="/servers/{server.pk}/stealth/"' in html

    response = admin_client.post(f"/servers/{server.pk}/stealth/")

    assert response.status_code == 302
    assert Operation.objects.filter(kind=stealth.KIND, params__server_id=server.pk).exists()


def test_the_card_shows_the_findings_with_advice(admin_client: Client, server: Server) -> None:
    findings: list[Any] = [
        StealthFinding(StealthItem.PORTS, StealthStatus.WARN, "tcp 8080 (nginx)"),
        StealthFinding(StealthItem.IPV6, StealthStatus.OK),
    ]
    stealth.save(server, findings)

    html = admin_client.get(f"/servers/{server.pk}/").content.decode()

    assert "tcp 8080 (nginx)" in html
    assert "close their ports in the firewall" in html
    assert "1 to fix" in html
