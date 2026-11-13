"""Key operations on an XRay instance: the key is a VLESS client of server.json."""

from collections.abc import Iterator
from contextlib import nullcontext
from typing import Any

import pytest
from django.db import connection

from amnezia_core.clients_table import LAMNEZIA_USER_ID, ClientsTable
from amnezia_core.vpn_link import decode_vpn_link
from amnezia_core.xray import (
    XRAY,
    XRAY_CLIENTS_TABLE_PATH,
    XRAY_CONFIG_PATH,
    XRAY_OWN_CLIENT_PATH,
    XRAY_PUBLIC_KEY_PATH,
    XrayServerConfig,
    client_fingerprint,
)
from apps.accounts.tests.factories import UserFactory
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services import drift
from apps.keys.services.actions import KeyActions
from apps.keys.services.instances import Instance, instances
from apps.keys.services.profile import KeyExport, XrayKeyProfile
from apps.keys.services.xray_profiles import (
    BUILD_PROFILES,
    XrayProfiles,
    follow_short_id,
    request_profiles,
)
from apps.keys.tests.conftest import Run
from apps.keys.tests.fake_xray import FakeXrayServer
from apps.keys.tests.steps import FIXTURES
from apps.operations.models import Operation, OperationStatus

pytestmark = pytest.mark.django_db

SOURCE = "xray:1"
HOST = "203.0.113.20"
OWN_CLIENT = "6f0c3e52-1b7a-4c8e-9d2f-0a1b2c3d4e5f"
SERVER_PUBLIC_KEY = "Wv1cT4hZbYk0n3m8dQeR2sF5gJ7aL9pX6uI0oE1yK2c"
SHORT_ID = "a1b2c3d4e5f60718"


@pytest.fixture
def xray_server() -> Iterator[FakeXrayServer]:
    server = FakeXrayServer(
        {
            XRAY_CONFIG_PATH: (FIXTURES / "xray_server.json").read_text(encoding="utf-8"),
            XRAY_OWN_CLIENT_PATH: f"{OWN_CLIENT}\n",
            XRAY_PUBLIC_KEY_PATH: f"{SERVER_PUBLIC_KEY}\n",
        }
    )

    def provider() -> list[Instance]:
        return [Instance(SOURCE, "Frankfurt", HOST, lambda: nullcontext(server.host()), XRAY)]

    instances.register(provider)
    yield server
    instances.unregister(provider)


def client_ids(server: FakeXrayServer) -> list[str]:
    return XrayServerConfig.parse(server.files[XRAY_CONFIG_PATH]).client_ids


def clients_table(server: FakeXrayServer) -> ClientsTable:
    return ClientsTable.parse(server.files[XRAY_CLIENTS_TABLE_PATH])


def issued(actions: KeyActions, run: Run) -> AccessKey:
    run(lambda: actions.issue(SOURCE, "iPhone", UserFactory()))
    return AccessKey.objects.latest("pk")


def test_issue_adds_a_vless_client_found_by_its_fingerprint(
    xray_server: FakeXrayServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)

    assert key.status == KeyStatus.ACTIVE
    assert key.client_ip is None
    assert key.public_key == client_fingerprint(key.private_key)
    assert client_ids(xray_server) == [OWN_CLIENT, key.private_key]
    assert clients_table(xray_server).user_data(key.private_key)[LAMNEZIA_USER_ID] == key.user_id
    assert xray_server.restarts == 1


def test_uuid_is_stored_encrypted(
    xray_server: FakeXrayServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)

    with connection.cursor() as cursor:
        cursor.execute("SELECT private_key FROM keys_accesskey WHERE id = %s", [key.pk])
        (stored,) = cursor.fetchone()
    assert key.private_key not in stored


def test_issued_key_exports_vpn_and_vless_links(
    xray_server: FakeXrayServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)

    export = KeyExport.of(key)
    config = decode_vpn_link(export.vpn_link)

    assert export.files == ()
    assert config["hostName"] == HOST
    assert config["defaultContainer"] == "amnezia-xray"
    assert key.private_key in config["containers"][0]["xray"]["last_config"]
    assert export.vless_link is not None
    assert export.vless_link.startswith(f"vless://{key.private_key}@{HOST}:443?")
    assert f"pbk={SERVER_PUBLIC_KEY}" in export.vless_link
    assert "sid=a1b2c3d4e5f60718" in export.vless_link


def test_disable_and_enable_keep_the_uuid(
    xray_server: FakeXrayServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)

    run(lambda: actions.disable(key))
    assert key.private_key not in client_ids(xray_server)
    key.refresh_from_db()
    assert key.status == KeyStatus.DISABLED_MANUAL

    run(lambda: actions.enable(key))
    assert key.private_key in client_ids(xray_server)
    key.refresh_from_db()
    assert key.status == KeyStatus.ACTIVE


def test_revoke_removes_the_client_and_forgets_the_uuid(
    xray_server: FakeXrayServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)
    client_id = key.private_key

    run(lambda: actions.revoke(key))

    key.refresh_from_db()
    assert key.status == KeyStatus.REVOKED
    assert (key.private_key, key.profile) == ("", "")
    assert client_ids(xray_server) == [OWN_CLIENT]
    assert client_id not in clients_table(xray_server).client_ids


def test_rename_does_not_restart_xray(
    xray_server: FakeXrayServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)

    run(lambda: actions.rename(key, "Anna's phone"))

    assert clients_table(xray_server).name(key.private_key) == "Anna's phone"
    assert xray_server.restarts == 1


def test_assign_writes_the_owner(
    xray_server: FakeXrayServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)
    owner = UserFactory()

    run(lambda: actions.assign(key, owner))

    assert clients_table(xray_server).user_data(key.private_key)[LAMNEZIA_USER_ID] == owner.pk


def change_short_id(server: FakeXrayServer, short_id: str) -> None:
    server.files[XRAY_CONFIG_PATH] = server.files[XRAY_CONFIG_PATH].replace(SHORT_ID, short_id)


def imported(status: str = KeyStatus.ACTIVE, client_id: str = OWN_CLIENT) -> AccessKey:
    """A key the import found in server.json: its UUID is known, its profile is not."""
    return AccessKey.objects.create(
        source=SOURCE,
        name="Installer's client",
        public_key=client_fingerprint(client_id),
        private_key=client_id,
        status=status,
    )


def short_id_of(key: AccessKey) -> str:
    key.refresh_from_db()
    return XrayKeyProfile.from_json(key.profile).short_id


def test_reconcile_only_reads_xray(
    xray_server: FakeXrayServer, actions: KeyActions, run: Run
) -> None:
    issued(actions, run)
    config = xray_server.files[XRAY_CONFIG_PATH]

    drift.reconcile_all()

    assert xray_server.files[XRAY_CONFIG_PATH] == config
    assert xray_server.restarts == 1


def test_reconcile_brings_a_new_short_id_into_the_profiles(
    xray_server: FakeXrayServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)
    change_short_id(xray_server, "0011223344556677")

    drift.reconcile_all()

    assert short_id_of(key) == "0011223344556677"
    assert "sid=0011223344556677" in (KeyExport.of(key).vless_link or "")


def test_issue_with_a_new_short_id_updates_the_other_profiles(
    xray_server: FakeXrayServer, actions: KeyActions, run: Run
) -> None:
    first = issued(actions, run)
    change_short_id(xray_server, "0011223344556677")

    second = issued(actions, run)

    assert short_id_of(first) == short_id_of(second) == "0011223344556677"


def test_revoked_keys_keep_their_profile_out_of_it(
    xray_server: FakeXrayServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)
    run(lambda: actions.revoke(key))
    change_short_id(xray_server, "0011223344556677")

    assert follow_short_id(SOURCE, "0011223344556677") == 0


def test_an_imported_key_gets_its_profile_in_the_background(
    xray_server: FakeXrayServer, django_capture_on_commit_callbacks: Any
) -> None:
    key = imported()
    assert key.awaits_profile

    with django_capture_on_commit_callbacks(execute=True):
        assert request_profiles([key])

    key.refresh_from_db()
    assert not key.awaits_profile
    assert key.can_export
    export = KeyExport.of(key)
    assert export.vless_link is not None
    assert export.vless_link.startswith(f"vless://{OWN_CLIENT}@{HOST}:443?")
    assert f"sid={SHORT_ID}" in export.vless_link
    assert not request_profiles([key])


def test_a_disabled_imported_key_gets_its_profile_too(xray_server: FakeXrayServer) -> None:
    key = imported(KeyStatus.DISABLED_MANUAL)

    with instances.get(SOURCE).connect() as host:
        assert XrayProfiles(instances.get(SOURCE), host).fill() == 1

    key.refresh_from_db()
    assert key.profile


def test_one_build_at_a_time_per_instance(xray_server: FakeXrayServer) -> None:
    keys = [imported(), imported(client_id="0b9e1f7a-2c3d-4e5f-8a9b-1c2d3e4f5a6b")]

    assert request_profiles(keys)
    assert request_profiles(keys)

    assert Operation.objects.filter(kind=BUILD_PROFILES).count() == 1


def test_a_failed_build_is_not_retried_at_once(xray_server: FakeXrayServer) -> None:
    key = imported()
    request_profiles([key])
    Operation.objects.filter(kind=BUILD_PROFILES).update(status=OperationStatus.FAILED)

    assert not request_profiles([key])
    assert Operation.objects.filter(kind=BUILD_PROFILES).count() == 1


def test_keys_with_a_profile_need_no_build(
    xray_server: FakeXrayServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)

    assert not request_profiles([key])
    assert not Operation.objects.filter(kind=BUILD_PROFILES).exists()
