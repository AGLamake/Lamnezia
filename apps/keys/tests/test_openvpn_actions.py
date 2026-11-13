"""Key operations on an OpenVPN instance: the key is a client certificate of the easy-rsa CA."""

from collections.abc import Callable, Iterator
from contextlib import nullcontext

import pytest
from django.db import connection

from amnezia_core.clients_table import LAMNEZIA_USER_ID, ClientsTable
from amnezia_core.openvpn import (
    OPENVPN,
    OPENVPN_CLIENTS_TABLE_PATH,
    certificate_path,
    certificate_serial,
    is_valid_client_id,
)
from amnezia_core.vpn_link import decode_vpn_link
from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services import drift
from apps.keys.services.actions import KeyActions
from apps.keys.services.instances import Instance, instances
from apps.keys.services.profile import KeyExport
from apps.keys.services.reissue import KeyReissue, ReissueStart
from apps.keys.tests.conftest import OnCommit, Run
from apps.keys.tests.fake_openvpn import TA_KEY, FakeOpenVpnServer
from apps.operations.models import Operation, OperationStatus

pytestmark = pytest.mark.django_db

SOURCE = "openvpn:1"
HOST = "203.0.113.30"
DNS = ("1.1.1.1", "1.0.0.1")


@pytest.fixture
def openvpn_server() -> Iterator[FakeOpenVpnServer]:
    server = FakeOpenVpnServer()

    def provider() -> list[Instance]:
        connect = lambda: nullcontext(server.host())  # noqa: E731
        return [Instance(SOURCE, "Frankfurt", HOST, connect, OPENVPN, dns=DNS)]

    instances.register(provider)
    yield server
    instances.unregister(provider)


def clients_table(server: FakeOpenVpnServer) -> ClientsTable:
    return ClientsTable.parse(server.files[OPENVPN_CLIENTS_TABLE_PATH])


def issued(actions: KeyActions, run: Run) -> AccessKey:
    run(lambda: actions.issue(SOURCE, "iPhone", UserFactory()))
    return AccessKey.objects.latest("pk")


Start = Callable[[Callable[[], ReissueStart]], list[Operation]]


@pytest.fixture
def start(django_capture_on_commit_callbacks: OnCommit) -> Start:
    def runner(begin: Callable[[], ReissueStart]) -> list[Operation]:
        with django_capture_on_commit_callbacks(execute=True):
            started = begin()
        for operation in started.operations:
            operation.refresh_from_db()
        return started.operations

    return runner


def test_issue_signs_a_certificate_named_by_the_key(
    openvpn_server: FakeOpenVpnServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)

    assert key.status == KeyStatus.ACTIVE
    assert key.client_ip is None
    assert is_valid_client_id(key.public_key)
    assert certificate_path(key.public_key) in openvpn_server.files
    assert clients_table(openvpn_server).user_data(key.public_key)[LAMNEZIA_USER_ID] == (
        key.user_id
    )
    assert openvpn_server.restarts == 0


def test_private_key_and_profile_are_stored_encrypted(
    openvpn_server: FakeOpenVpnServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)

    with connection.cursor() as cursor:
        cursor.execute("SELECT private_key, profile FROM keys_accesskey WHERE id = %s", [key.pk])
        stored = " ".join(cursor.fetchone())
    assert "PRIVATE KEY" not in stored
    assert "Static key" not in stored


def test_issued_key_exports_vpn_link_and_ovpn(
    openvpn_server: FakeOpenVpnServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)

    export = KeyExport.of(key)
    config = decode_vpn_link(export.vpn_link)

    assert (config["hostName"], config["defaultContainer"]) == (HOST, "amnezia-openvpn")
    assert (config["dns1"], config["dns2"]) == DNS
    assert [file.extension for file in export.files] == ["ovpn"]
    assert export.vless_link is None
    ovpn = export.files[0].content
    assert f"remote {HOST} 1194" in ovpn
    assert openvpn_server.ca.pem.strip() in ovpn
    assert openvpn_server.files[certificate_path(key.public_key)].strip() in ovpn
    assert key.private_key.strip() in ovpn
    assert "dhcp-option DNS 1.1.1.1" in ovpn
    assert "<tls-auth>" in ovpn and TA_KEY.splitlines()[0] in ovpn


def test_disable_and_enable_go_through_the_crl(
    openvpn_server: FakeOpenVpnServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)
    serial = certificate_serial(openvpn_server.files[certificate_path(key.public_key)])

    run(lambda: actions.disable(key))
    key.refresh_from_db()
    assert key.status == KeyStatus.DISABLED_MANUAL
    assert openvpn_server.refused() == {serial}

    run(lambda: actions.enable(key))
    key.refresh_from_db()
    assert key.status == KeyStatus.ACTIVE
    assert openvpn_server.refused() == set()
    assert KeyExport.of(key).files  # the same profile still works


def test_revoke_puts_the_certificate_in_the_crl_and_forgets_the_key(
    openvpn_server: FakeOpenVpnServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)
    client_id = key.public_key
    serial = certificate_serial(openvpn_server.files[certificate_path(client_id)])

    run(lambda: actions.revoke(key))

    key.refresh_from_db()
    assert key.status == KeyStatus.REVOKED
    assert (key.private_key, key.profile) == ("", "")
    assert openvpn_server.refused() == {serial}
    assert client_id not in clients_table(openvpn_server)


def test_rename_does_not_restart_openvpn(
    openvpn_server: FakeOpenVpnServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)

    run(lambda: actions.rename(key, "Anna's phone"))

    assert clients_table(openvpn_server).name(key.public_key) == "Anna's phone"
    assert openvpn_server.restarts == 0


def test_drift_check_skips_openvpn(
    openvpn_server: FakeOpenVpnServer, actions: KeyActions, run: Run
) -> None:
    issued(actions, run)
    commands = len(openvpn_server.easyrsa)
    restarts = openvpn_server.restarts

    drift.reconcile_all()

    assert (len(openvpn_server.easyrsa), openvpn_server.restarts) == (commands, restarts)


def test_batch_reissue_replaces_the_certificates_in_one_step(
    openvpn_server: FakeOpenVpnServer, actions: KeyActions, run: Run, start: Start
) -> None:
    olds = [issued(actions, run), issued(actions, run)]

    (operation,) = start(lambda: KeyReissue(UserFactory(role=Role.ADMIN)).start(olds))

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    step = operation.steps.get(title="Replace the client certificates")
    for old in olds:
        old.refresh_from_db()
        assert old.status == KeyStatus.REVOKED
    assert step.log.count("iPhone: reissued.") == 2
    assert AccessKey.objects.filter(status=KeyStatus.ACTIVE).count() == 2
