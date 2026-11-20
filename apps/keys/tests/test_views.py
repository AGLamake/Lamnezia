from collections.abc import Iterator
from contextlib import AbstractContextManager, nullcontext

import pytest
from django.http import HttpResponse
from django.test import Client
from django.utils import timezone

from amnezia_core.keys import KeyPair
from amnezia_core.openvpn import ClientKey, OpenVpnSettings, new_client_id
from amnezia_core.tests.pki import FakeCa
from amnezia_core.xray import XRAY, client_fingerprint
from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.instances import Instance, instances
from apps.keys.services.profile import OpenVpnKeyProfile, ServerProfile, XrayKeyProfile
from apps.keys.services.xray_profiles import BUILD_PROFILES
from apps.keys.services.zone import KeyZone
from apps.keys.tests.conftest import HOST, PSK, SOURCE
from apps.keys.tests.fake_openvpn import TA_KEY
from apps.keys.tests.fake_server import FakeAwgServer
from apps.operations.models import Operation
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db


def issued_key(**fields: object) -> AccessKey:
    pair = KeyPair.generate()
    profile = ServerProfile(
        "amnezia-awg2", HOST, 55424, "10.8.1.0/24", {"Jc": "4"}, KeyPair.generate().public_key, PSK
    )
    defaults: dict[str, object] = {
        "source": SOURCE,
        "name": "iPhone",
        "public_key": pair.public_key,
        "private_key": pair.private_key,
        "profile": profile.to_json(),
        "client_ip": "10.8.1.5",
        "status": KeyStatus.ACTIVE,
    }
    return AccessKey.objects.create(**{**defaults, **fields})


def imported_key() -> AccessKey:
    return AccessKey.objects.create(
        source=SOURCE,
        name="laptop",
        public_key="imported",
        client_ip="10.8.1.9",
        status="active",
        imported=True,
    )


def test_list_filters_keys(admin_client: Client) -> None:
    owner = UserFactory(username="alice")
    issued_key(user=owner, name="alice-phone")
    issued_key(name="nobody-phone", status=KeyStatus.DISABLED_MANUAL)

    by_owner = admin_client.get("/keys/", {"owner": "ali"}).content.decode()
    orphans = admin_client.get("/keys/", {"no_owner": "on"}).content.decode()
    disabled = admin_client.get("/keys/", {"status": "disabled_manual"}).content.decode()

    assert "alice-phone" in by_owner and "nobody-phone" not in by_owner
    assert "nobody-phone" in orphans and "alice-phone" not in orphans
    assert "nobody-phone" in disabled and "alice-phone" not in disabled


def test_issue_form_starts_an_operation(admin_client: Client, awg_server: FakeAwgServer) -> None:
    owner = UserFactory()

    response = admin_client.post(
        "/keys/new/", {"user": owner.pk, "source": SOURCE, "name": "Pixel"}
    )

    operation = Operation.objects.get(kind="keys.issue")
    assert response.headers["Location"] == f"/operations/{operation.pk}/"
    key = AccessKey.objects.get()
    assert (key.user, key.name, key.status) == (owner, "Pixel", KeyStatus.ISSUING)


def test_issue_page_without_instances(admin_client: Client) -> None:
    html = admin_client.get("/keys/new/").content.decode()

    assert "No server can issue keys yet" in html


@pytest.mark.parametrize("action", ["disable", "revoke"])
def test_actions_start_operations(admin_client: Client, action: str) -> None:
    key = issued_key()

    response = admin_client.post(f"/keys/{key.pk}/{action}/")

    operation = Operation.objects.get(kind=f"keys.{action}")
    assert operation.params == {"key_id": key.pk}
    assert response.headers["Location"] == f"/operations/{operation.pk}/"


def test_action_not_allowed_in_the_current_status(admin_client: Client) -> None:
    key = issued_key()

    assert admin_client.post(f"/keys/{key.pk}/enable/").status_code == 404
    assert admin_client.post(f"/keys/{key.pk}/explode/").status_code == 404
    assert not Operation.objects.exists()


def test_assign_starts_an_operation(admin_client: Client) -> None:
    key = issued_key()
    owner = UserFactory()

    admin_client.post(f"/keys/{key.pk}/assign/", {"user": owner.pk})

    operation = Operation.objects.get(kind="keys.assign")
    assert operation.params == {"key_id": key.pk, "owner_id": owner.pk}


def test_admin_sees_the_key_and_the_look_is_logged(admin_client: Client) -> None:
    key = issued_key()

    html = admin_client.get(f"/keys/{key.pk}/show/").content.decode()
    conf = admin_client.get(f"/keys/{key.pk}/file/conf/")

    assert "vpn://" in html
    assert "<svg" in html
    assert f"PrivateKey = {key.private_key}" in conf.content.decode()
    assert conf.headers["Content-Disposition"].startswith("attachment")
    views = AuditEvent.objects.filter(action="keys.view", target_id=str(key.pk))
    assert views.count() == 2
    assert views.first().ip == "127.0.0.1"  # type: ignore[union-attr]
    key.refresh_from_db()
    assert key.first_viewed_at is None


def test_xray_key_shows_vless_and_has_no_conf(admin_client: Client) -> None:
    client_id = "0d4f7a2b-8c3e-4f1a-9b6d-5e2c1a0f9d8e"
    profile = XrayKeyProfile(
        HOST, 443, "xtls-rprx-vision", "www.example.com", "www.example.com", "chrome", "pbk", "ab"
    )
    key = issued_key(
        source="xray:1",
        public_key=client_fingerprint(client_id),
        private_key=client_id,
        profile=profile.to_json(),
        client_ip=None,
    )

    html = admin_client.get(f"/keys/{key.pk}/show/").content.decode()

    assert "vpn://" in html
    assert f"vless://{client_id}@{HOST}:443?" in html
    assert f"/keys/{key.pk}/file/" not in html
    assert admin_client.get(f"/keys/{key.pk}/file/conf/").status_code == 404


def test_openvpn_key_downloads_an_ovpn(admin_client: Client) -> None:
    ca = FakeCa()
    client_id = new_client_id()
    client_key = ClientKey.generate()
    certificate = ca.sign(client_key.certificate_request(client_id), 0x11)
    profile = OpenVpnKeyProfile.build(HOST, OpenVpnSettings(), ca.pem, certificate, TA_KEY)
    key = issued_key(
        source="openvpn:1",
        public_key=client_id,
        private_key=client_key.pem,
        profile=profile.to_json(),
        client_ip=None,
    )

    html = admin_client.get(f"/keys/{key.pk}/show/").content.decode()
    conf = admin_client.get(f"/keys/{key.pk}/file/ovpn/")

    assert "Download .ovpn for OpenVPN" in html
    assert f'filename="lamnezia-{key.pk}.ovpn"' in conf.headers["Content-Disposition"]
    assert f"remote {HOST} 1194" in conf.content.decode()


@pytest.mark.parametrize("path", ["show", "file/conf"])
def test_imported_key_cannot_be_shown(admin_client: Client, path: str) -> None:
    key = imported_key()

    assert admin_client.get(f"/keys/{key.pk}/{path}/").status_code == 403
    assert "cannot be shown" in admin_client.get(f"/keys/{key.pk}/").content.decode()


def test_imported_xray_key_is_prepared_for_showing(admin_client: Client) -> None:
    def provider() -> list[Instance]:
        return [Instance("xray:1", "Frankfurt", HOST, nullcontext, XRAY)]  # type: ignore[arg-type]

    key = issued_key(source="xray:1", private_key="uuid", profile="")
    instances.register(provider)
    try:
        page = admin_client.get(f"/keys/{key.pk}/").content.decode()
    finally:
        instances.unregister(provider)

    assert "The panel is reading the key" in page
    assert Operation.objects.get().kind == BUILD_PROFILES


def test_reset_first_view(admin_client: Client) -> None:
    key = issued_key(first_viewed_at=timezone.now())

    admin_client.post(f"/keys/{key.pk}/reset-view/")

    key.refresh_from_db()
    assert key.first_viewed_at is None


def test_keys_are_for_staff_only(client: Client) -> None:
    key = issued_key()
    client.force_login(UserFactory(role=Role.USER))

    for path in (
        "/keys/",
        f"/keys/{key.pk}/",
        f"/keys/{key.pk}/show/",
        f"/keys/{key.pk}/file/conf/",
    ):
        assert client.get(path).status_code == 403
    assert client.post(f"/keys/{key.pk}/revoke/").status_code == 403


def test_list_names_the_server_not_the_instance_id(admin_client: Client) -> None:
    def provider() -> list[Instance]:
        return [Instance("xray:1", "Frankfurt · XRay", HOST, nullcontext, XRAY)]  # type: ignore[arg-type]

    issued_key(source="xray:1")
    instances.register(provider)
    try:
        page = admin_client.get("/keys/").content.decode()
    finally:
        instances.unregister(provider)

    assert "Frankfurt · XRay" in page
    assert ">xray:1<" not in page


class TestModeratorKeyQuota:
    """A moderator's quota of one key, on a server given to the moderator."""

    SOURCE = "awg:7"

    @pytest.fixture
    def moderator(self) -> Iterator[User]:
        moderator = UserFactory(role=Role.MODERATOR, key_quota=1)
        server = ServerFactory()
        moderator.moderated_servers.add(server)

        def provider() -> list[Instance]:
            return [Instance(self.SOURCE, "Amsterdam", HOST, _no_host, server_id=server.pk)]

        instances.register(provider)
        yield moderator
        instances.unregister(provider)

    def issue(self, client: Client, owner: User) -> HttpResponse:
        data = {"user": owner.pk, "source": self.SOURCE, "name": "Pixel"}
        return client.post("/keys/new/", data)  # type: ignore[return-value]

    def test_a_key_within_the_quota_is_issued(self, client: Client, moderator: User) -> None:
        client.force_login(moderator)

        self.issue(client, UserFactory(managers=[moderator]))

        assert AccessKey.objects.filter(status=KeyStatus.ISSUING).count() == 1

    def test_a_key_issued_meanwhile_by_another_request_stops_this_one(
        self, client: Client, moderator: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        owner = UserFactory(managers=[moderator])
        client.force_login(moderator)
        # Both requests passed the form while the quota had room; the other one issued first.
        monkeypatch.setattr(KeyZone, "quota_reached", lambda zone: False)
        issued_key(user=owner, source=self.SOURCE)

        response = self.issue(client, owner)

        assert response.status_code == 200
        assert "Your users hold as many keys as your quota allows." in response.content.decode()
        assert AccessKey.objects.count() == 1
        assert not Operation.objects.exists()


def _no_host() -> AbstractContextManager[RemoteHost]:
    raise AssertionError("issuing a key does not connect to the server")
