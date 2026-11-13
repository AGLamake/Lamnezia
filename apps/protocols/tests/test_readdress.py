import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

import pytest
from django.test import Client

from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.reissue import REISSUE
from apps.notifications.events import Event
from apps.notifications.models import Notification
from apps.operations.models import Operation
from apps.protocols.models import InstanceOrigin, ProtocolInstance
from apps.protocols.services.readdress import READDRESS, ServerReaddress
from apps.servers.models import AuthMethod
from apps.servers.tests.factories import PRIVATE_KEY, ServerFactory

pytestmark = pytest.mark.django_db

OnCommit = Callable[..., AbstractContextManager[Any]]
OLD, NEW = "203.0.113.10", "vpn.example.com"


@pytest.fixture
def instance() -> ProtocolInstance:
    server = ServerFactory(name="Frankfurt", host=NEW)
    return ProtocolInstance.objects.create(
        server=server,
        container="amnezia-awg2",
        port=55424,
        subnet="10.8.1.0/24",
        public_key="server-public",
        private_key="server-private",
        origin=InstanceOrigin.IMPORTED,
    )


def issued(instance: ProtocolInstance, host: str, **fields: Any) -> AccessKey:
    profile = json.dumps({"container": "amnezia-awg2", "host": host, "port": 55424})
    return AccessKey.objects.create(
        source=instance.source,
        name="iPhone",
        public_key=f"peer-{AccessKey.objects.count()}",
        private_key="client-private",
        profile=profile,
        **{"status": KeyStatus.ACTIVE, **fields},
    )


def imported(instance: ProtocolInstance, **fields: Any) -> AccessKey:
    return AccessKey.objects.create(
        source=instance.source,
        name="Laptop",
        public_key=f"peer-{AccessKey.objects.count()}",
        imported=True,
        **{"status": KeyStatus.ACTIVE, **fields},
    )


def host_of(key: AccessKey) -> str:
    key.refresh_from_db()
    return str(json.loads(key.profile)["host"])


def test_profiles_move_to_the_new_address_and_owners_are_told(
    django_capture_on_commit_callbacks: OnCommit, instance: ProtocolInstance
) -> None:
    owner = UserFactory()
    moved = issued(instance, OLD, user=owner)
    current = issued(instance, NEW)
    issued(instance, OLD, status=KeyStatus.REVOKED)

    with django_capture_on_commit_callbacks(execute=True):
        result = ServerReaddress(instance.server).apply(UserFactory(), reissue=False)

    assert (result.moved, result.reissued, result.owners) == (1, 0, 1)
    assert host_of(moved) == NEW
    assert host_of(current) == NEW
    notification = Notification.objects.get(event=Event.KEYS_CHANGED)
    assert (notification.user, notification.data["server"]) == (owner, "Frankfurt")
    assert AuditEvent.objects.get(action=READDRESS).data["moved"] == 1


def test_imported_keys_are_reissued_only_when_asked(instance: ProtocolInstance) -> None:
    imported(instance)

    kept = ServerReaddress(instance.server).apply(UserFactory(), reissue=False)
    assert (kept.reissued, kept.kept) == (0, 1)
    assert not Operation.objects.filter(kind=REISSUE).exists()

    reissued = ServerReaddress(instance.server).apply(UserFactory(), reissue=True)
    assert reissued.reissued == 1
    assert Operation.objects.filter(kind=REISSUE).count() == 1


def test_nothing_pending_when_every_key_has_the_address(instance: ProtocolInstance) -> None:
    issued(instance, NEW)

    assert not ServerReaddress(instance.server).pending


def form_data(**overrides: Any) -> dict[str, Any]:
    return {
        "name": "Frankfurt",
        "host": NEW,
        "port": 22,
        "username": "root",
        "auth_method": AuthMethod.KEY,
        "private_key": PRIVATE_KEY,
        "passphrase": "",
        "password": "",
        "location": "",
        "paid_until": "",
        **overrides,
    }


def test_changing_the_address_offers_to_update_the_keys(
    admin_client: Client, instance: ProtocolInstance
) -> None:
    server = instance.server
    server.host = OLD
    server.save(update_fields=["host"])
    key = issued(instance, OLD)
    imported(instance)

    response = admin_client.post(f"/servers/{server.pk}/edit/", form_data())

    assert response["Location"] == f"/protocols/readdress/{server.pk}/"
    page = admin_client.get(response["Location"]).content.decode()
    assert NEW in page
    assert 'name="reissue"' in page

    response = admin_client.post(f"/protocols/readdress/{server.pk}/", {"reissue": "on"})

    assert response["Location"] == f"/servers/{server.pk}/"
    assert host_of(key) == NEW
    assert Operation.objects.filter(kind=REISSUE).count() == 1


def test_changing_the_address_without_keys_goes_to_the_card(
    admin_client: Client, instance: ProtocolInstance
) -> None:
    server = instance.server

    response = admin_client.post(f"/servers/{server.pk}/edit/", form_data(host=OLD))

    assert response["Location"] == f"/servers/{server.pk}/"
