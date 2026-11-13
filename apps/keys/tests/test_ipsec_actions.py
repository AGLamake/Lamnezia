"""Key operations on an IPsec instance: the key is a client certificate made in the container."""

import base64
import json
from collections.abc import Callable, Iterator
from contextlib import nullcontext

import pytest
from django.db import connection
from django.test import Client

from amnezia_core.clients_table import LAMNEZIA_USER_ID, ClientsTable
from amnezia_core.ipsec import IPSEC, IPSEC_CLIENTS_TABLE_PATH, is_valid_client_id
from amnezia_core.vpn_link import decode_vpn_link
from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.actions import KeyActions
from apps.keys.services.instances import Instance, instances
from apps.keys.services.profile import KeyExport
from apps.keys.services.reissue import KeyReissue, ReissueStart
from apps.keys.tests.conftest import OnCommit, Run
from apps.keys.tests.fake_ipsec import P12, SERVER_IP, FakeIpsecServer
from apps.operations.models import Operation, OperationStatus

pytestmark = pytest.mark.django_db

SOURCE = "ipsec:1"
HOST = "vpn.example.com"
DNS = ("1.1.1.1", "1.0.0.1")


@pytest.fixture
def ipsec_server() -> Iterator[FakeIpsecServer]:
    server = FakeIpsecServer()

    def provider() -> list[Instance]:
        connect = lambda: nullcontext(server.host())  # noqa: E731
        return [Instance(SOURCE, "Frankfurt", HOST, connect, IPSEC, dns=DNS)]

    instances.register(provider)
    yield server
    instances.unregister(provider)


def clients_table(server: FakeIpsecServer) -> ClientsTable:
    return ClientsTable.parse(server.files[IPSEC_CLIENTS_TABLE_PATH].decode())


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


def test_issue_keeps_the_exported_pkcs12_as_the_private_key(
    ipsec_server: FakeIpsecServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)

    assert key.status == KeyStatus.ACTIVE
    assert is_valid_client_id(key.public_key)
    assert ipsec_server.signed == [key.public_key]
    assert base64.b64decode(key.private_key) == P12 + key.public_key.encode()
    assert clients_table(ipsec_server).user_data(key.public_key)[LAMNEZIA_USER_ID] == key.user_id


def test_pkcs12_is_stored_encrypted(
    ipsec_server: FakeIpsecServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)

    with connection.cursor() as cursor:
        cursor.execute("SELECT private_key FROM keys_accesskey WHERE id = %s", [key.pk])
        assert key.private_key not in cursor.fetchone()[0]


def test_issued_key_exports_the_app_link_and_the_device_profiles(
    ipsec_server: FakeIpsecServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)

    export = KeyExport.of(key)

    config = decode_vpn_link(export.vpn_link)
    assert (config["hostName"], config["defaultContainer"]) == (HOST, "amnezia-ipsec")
    assert (config["dns1"], config["dns2"]) == DNS
    last_config = json.loads(config["containers"][0]["ikev2"]["last_config"])
    assert (last_config["userName"], last_config["cert"]) == (key.public_key, key.private_key)
    assert [file.extension for file in export.files] == ["sswan", "mobileconfig"]
    sswan = json.loads(export.files[0].content)
    assert sswan["remote"]["addr"] == SERVER_IP
    assert f"<string>{key.public_key}</string>" in export.files[1].content


def test_disable_enable_and_revoke_go_through_the_crl(
    ipsec_server: FakeIpsecServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)
    serial = ipsec_server.certificates[key.public_key]

    run(lambda: actions.disable(key))
    assert ipsec_server.loaded_crl == {serial}

    run(lambda: actions.enable(key))
    assert ipsec_server.loaded_crl == set()

    run(lambda: actions.revoke(key))
    key.refresh_from_db()
    assert key.status == KeyStatus.REVOKED
    assert (key.private_key, key.profile) == ("", "")
    assert ipsec_server.loaded_crl == {serial}
    assert key.public_key not in ipsec_server.certificates


def test_admin_downloads_the_apple_profile(
    ipsec_server: FakeIpsecServer, actions: KeyActions, run: Run, admin_client: Client
) -> None:
    key = issued(actions, run)

    html = admin_client.get(f"/keys/{key.pk}/show/").content.decode()
    response = admin_client.get(f"/keys/{key.pk}/file/mobileconfig/")

    assert "Download .sswan for strongSwan on Android" in html
    assert response["Content-Type"].startswith("application/x-apple-aspen-config")
    assert f'filename="lamnezia-{key.pk}.mobileconfig"' in response["Content-Disposition"]


def test_batch_reissue_replaces_the_certificates_in_one_step(
    ipsec_server: FakeIpsecServer, actions: KeyActions, run: Run, start: Start
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
