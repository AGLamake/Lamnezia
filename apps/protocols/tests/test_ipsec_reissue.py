"""The IPsec server certificate is issued for the new server IP, and the key profiles follow."""

import socket
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

import pytest
from django.test import Client

from amnezia_core.ipsec import IKEV2_CONFIG_PATH
from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.actions import ISSUE
from apps.keys.services.profile import IpsecKeyProfile
from apps.notifications.events import Event
from apps.notifications.models import Notification
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.launcher import launch
from apps.protocols.models import InstanceOrigin, ProtocolInstance
from apps.protocols.services.ipsec_reissue import REISSUE_KIND, IpsecIdentity, start_reissue
from apps.protocols.tests.fake_ipsec_host import FakeIpsecHost
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db

OLD, NEW = "203.0.113.7", "198.51.100.9"
OnCommit = Callable[..., AbstractContextManager[Any]]
Start = Callable[[Callable[[], Operation]], Operation]


def ikev2_conf(server_ip: str) -> bytes:
    return f"conn ikev2-cp\n  leftcert={server_ip}\n  leftid={server_ip}\n  right=%any\n".encode()


def ipsec_connect(server: FakeIpsecHost) -> Callable[[Server], AbstractContextManager[RemoteHost]]:
    @contextmanager
    def connect(_server: Server) -> Iterator[RemoteHost]:
        yield server.host()

    return connect


@pytest.fixture
def ipsec(monkeypatch: pytest.MonkeyPatch) -> FakeIpsecHost:
    server = FakeIpsecHost(
        container_state="running",
        has_ca=True,
        pluto_up=True,
        files={IKEV2_CONFIG_PATH: ikev2_conf(OLD)},
        server_certificates={OLD},
    )
    monkeypatch.setattr("apps.protocols.operations.connect", ipsec_connect(server))
    return server


@pytest.fixture
def instance() -> ProtocolInstance:
    return ProtocolInstance.objects.create(
        server=ServerFactory(name="Frankfurt", host=NEW),
        container="amnezia-ipsec",
        port=500,
        subnet="192.168.43.0/24",
        params={"server_ip": OLD},
        public_key="ca-fingerprint",
        origin=InstanceOrigin.INSTALLED,
    )


@pytest.fixture
def admin() -> User:
    return UserFactory(role=Role.ADMIN)


@pytest.fixture
def start(django_capture_on_commit_callbacks: OnCommit) -> Start:
    def runner(launch: Callable[[], Operation]) -> Operation:
        with django_capture_on_commit_callbacks(execute=True):
            operation = launch()
        operation.refresh_from_db()
        return operation

    return runner


def issued_key(instance: ProtocolInstance, owner: User | None = None) -> AccessKey:
    profile = IpsecKeyProfile(OLD, OLD, "Q0E=")
    return AccessKey.objects.create(
        user=owner,
        source=instance.source,
        name="iPhone",
        public_key=f"Client{AccessKey.objects.count()}",
        private_key="cDEy",
        profile=profile.to_json(),
        status=KeyStatus.ACTIVE,
    )


def test_certificate_for_the_new_ip_replaces_the_old_one(
    ipsec: FakeIpsecHost, instance: ProtocolInstance, admin: User, start: Start
) -> None:
    operation = start(lambda: start_reissue(instance, admin, reissue=False))

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    assert ipsec.server_certificates == {NEW}
    assert ipsec.files[IKEV2_CONFIG_PATH] == ikev2_conf(NEW)
    assert ipsec.pluto_up and ipsec.service_runs == 1
    assert ipsec.pki_runs == 0, "the CA and the client certificates are kept"
    instance.refresh_from_db()
    assert instance.params == {"server_ip": NEW}
    assert AuditEvent.objects.get(action=REISSUE_KIND).data["host"] == NEW


def test_the_restarted_pluto_gets_the_new_leftid_in_ipsec_conf(
    ipsec: FakeIpsecHost, instance: ProtocolInstance, admin: User, start: Start
) -> None:
    start(lambda: start_reissue(instance, admin, reissue=False))

    [script] = ipsec.configure_scripts
    assert f"leftid={NEW}\n" in script and OLD not in script


def test_key_profiles_name_the_new_ip_and_the_owners_are_told(
    ipsec: FakeIpsecHost, instance: ProtocolInstance, admin: User, start: Start
) -> None:
    key = issued_key(instance, UserFactory(email="anna@example.com"))
    revoked = issued_key(instance)
    revoked.status = KeyStatus.REVOKED
    revoked.save()

    start(lambda: start_reissue(instance, admin, reissue=False))

    key.refresh_from_db()
    profile = IpsecKeyProfile.from_json(key.profile)
    assert (profile.host, profile.server_ip) == (NEW, NEW)
    assert key.private_key == "cDEy", "the client certificate stays valid"
    revoked.refresh_from_db()
    assert IpsecKeyProfile.from_json(revoked.profile).server_ip == OLD
    assert Notification.objects.get(event=Event.KEYS_CHANGED).user == key.user


def test_a_key_issued_during_the_reissue_names_the_new_ip(
    ipsec: FakeIpsecHost,
    instance: ProtocolInstance,
    admin: User,
    start: Start,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key reads the old IP from the server, the reissue finishes before the key is saved."""
    key = AccessKey.objects.create(
        source=instance.source, name="iPad", public_key="Client9", status=KeyStatus.ISSUING
    )

    class Backend:
        def issue(self, issued: AccessKey) -> None:
            issued.private_key = "cDEy"
            issued.profile = IpsecKeyProfile(OLD, OLD, "Q0E=").to_json()
            start(lambda: start_reissue(instance, admin, reissue=False))

    monkeypatch.setattr("apps.protocols.providers.connect", ipsec_connect(ipsec))
    monkeypatch.setattr("apps.keys.operations.backend_for", lambda *_args: Backend())

    operation = start(lambda: launch(ISSUE, {"key_id": key.pk}, user=admin))

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    key.refresh_from_db()
    profile = IpsecKeyProfile.from_json(key.profile)
    assert (profile.host, profile.server_ip) == (NEW, NEW)
    assert key.status == KeyStatus.ACTIVE


def test_a_certificate_already_issued_for_the_ip_is_kept(
    ipsec: FakeIpsecHost, instance: ProtocolInstance, admin: User, start: Start
) -> None:
    ipsec.files[IKEV2_CONFIG_PATH] = ikev2_conf(NEW)
    ipsec.server_certificates = {NEW}
    ipsec.pluto_up = False

    operation = start(lambda: start_reissue(instance, admin, reissue=False))

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    assert ipsec.server_certificates == {NEW}
    assert ipsec.pluto_up
    instance.refresh_from_db()
    assert instance.params == {"server_ip": NEW}


def test_failed_signing_leaves_the_server_as_it_was(
    ipsec: FakeIpsecHost, instance: ProtocolInstance, admin: User, start: Start
) -> None:
    key = issued_key(instance)
    ipsec.certutil_fails = True

    operation = start(lambda: start_reissue(instance, admin, reissue=False))

    assert operation.status == OperationStatus.FAILED
    assert ipsec.server_certificates == {OLD}
    assert ipsec.files[IKEV2_CONFIG_PATH] == ikev2_conf(OLD)
    instance.refresh_from_db()
    assert instance.params == {"server_ip": OLD}
    key.refresh_from_db()
    assert IpsecKeyProfile.from_json(key.profile).server_ip == OLD


def test_a_retry_after_a_failed_restart_finishes_the_job(
    ipsec: FakeIpsecHost, instance: ProtocolInstance, admin: User, start: Start
) -> None:
    ipsec.pluto_starts = False
    assert (
        start(lambda: start_reissue(instance, admin, reissue=False)).status
        == OperationStatus.FAILED
    )
    ipsec.pluto_starts = True

    operation = start(lambda: start_reissue(instance, admin, reissue=False))

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    assert ipsec.files[IKEV2_CONFIG_PATH] == ikev2_conf(NEW)
    assert ipsec.pluto_up
    instance.refresh_from_db()
    assert instance.params == {"server_ip": NEW}


def test_a_server_given_by_name_is_resolved(
    ipsec: FakeIpsecHost,
    instance: ProtocolInstance,
    admin: User,
    start: Start,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Server.objects.filter(pk=instance.server_id).update(host="vpn.example.com")
    instance.refresh_from_db()
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *_args: [(socket.AF_INET, 0, 0, "", (NEW, 0))]
    )

    operation = start(lambda: start_reissue(instance, admin, reissue=False))

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    assert ipsec.server_certificates == {NEW}


@pytest.mark.parametrize(
    ("host", "outdated"), [(OLD, False), (NEW, True), ("vpn.example.com", False)]
)
def test_identity_tells_whether_the_certificate_fits(
    instance: ProtocolInstance, host: str, *, outdated: bool
) -> None:
    Server.objects.filter(pk=instance.server_id).update(host=host)
    identity = IpsecIdentity.of(Server.objects.get(pk=instance.server_id))

    assert identity is not None
    assert (identity.issued_for, identity.outdated) == (OLD, outdated)


def test_a_removed_container_has_no_identity(instance: ProtocolInstance) -> None:
    instance.container_removed_at = instance.created_at
    instance.save()

    assert IpsecIdentity.of(instance.server) is None


@pytest.fixture
def admin_client(client: Client, admin: User) -> Client:
    client.force_login(admin)
    return client


def test_readdress_warns_of_ipsec_and_starts_the_reissue(
    admin_client: Client, instance: ProtocolInstance
) -> None:
    url = f"/protocols/readdress/{instance.server_id}/"
    page = admin_client.get(url).content.decode()
    assert "IPsec" in page and 'name="ipsec"' in page

    response = admin_client.post(url, {"ipsec": "on"})

    assert response["Location"] == f"/servers/{instance.server_id}/"
    assert Operation.objects.get().kind == REISSUE_KIND


def test_readdress_without_the_box_leaves_the_certificate(
    admin_client: Client, instance: ProtocolInstance
) -> None:
    admin_client.post(f"/protocols/readdress/{instance.server_id}/", {})

    assert not Operation.objects.filter(kind=REISSUE_KIND).exists()


def test_a_new_address_of_an_ipsec_server_goes_to_readdress(
    admin_client: Client, instance: ProtocolInstance
) -> None:
    data = {
        "name": "Frankfurt",
        "host": "192.0.2.5",
        "port": 22,
        "username": "root",
        "auth_method": "key",
        "private_key": "",
        "passphrase": "",
        "password": "",
        "location": "",
        "paid_until": "",
    }

    response = admin_client.post(f"/servers/{instance.server_id}/edit/", data)

    assert response["Location"] == f"/protocols/readdress/{instance.server_id}/"


def test_card_tells_of_an_outdated_certificate_and_offers_the_reissue(
    admin_client: Client, instance: ProtocolInstance
) -> None:
    page = admin_client.get(f"/servers/{instance.server_id}/").content.decode()

    assert f"/protocols/{instance.pk}/ipsec/reissue/" in page
    assert f"but the server address is {NEW}" in page
    assert "On a VPS with another IP, reissue it on this card" in page, "the backup tells too"


def test_card_keeps_quiet_while_the_certificate_fits(
    admin_client: Client, instance: ProtocolInstance
) -> None:
    Server.objects.filter(pk=instance.server_id).update(host=OLD)

    page = admin_client.get(f"/servers/{instance.server_id}/").content.decode()

    assert "but the server address is" not in page
    assert f"/protocols/{instance.pk}/ipsec/reissue/" in page


def test_reissue_button_starts_the_operation(
    admin_client: Client, instance: ProtocolInstance
) -> None:
    response = admin_client.post(f"/protocols/{instance.pk}/ipsec/reissue/", {"reissue": "on"})

    assert response["Location"] == f"/servers/{instance.server_id}/"
    operation = Operation.objects.get(kind=REISSUE_KIND)
    assert operation.params["reissue"] is True


def test_reissue_of_another_protocol_is_not_found(
    admin_client: Client, instance: ProtocolInstance
) -> None:
    other = ProtocolInstance.objects.create(
        server=instance.server, container="amnezia-awg2", port=55424, subnet="10.8.1.0/24"
    )

    response = admin_client.post(f"/protocols/{other.pk}/ipsec/reissue/")

    assert response.status_code == 404
    assert not Operation.objects.exists()


def test_a_moderator_cannot_reissue(client: Client, instance: ProtocolInstance) -> None:
    client.force_login(UserFactory(role=Role.MODERATOR))

    client.post(f"/protocols/{instance.pk}/ipsec/reissue/")

    assert not Operation.objects.exists()
