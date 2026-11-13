import pytest
from django.test import Client

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.operations.models import Operation
from apps.servers.services.check import CHECK_KIND
from apps.servers.services.host_key import FORGET_HOST_KEY
from apps.servers.tests.factories import ServerFactory
from apps.ssh.models import KnownHost

pytestmark = pytest.mark.django_db

FINGERPRINT = "SHA256:nThbg6kXUpJWGl7E1IGOCspRomTxdCARLviKw6E5SY8"


@pytest.fixture
def admin_client(client: Client) -> Client:
    client.force_login(UserFactory(role=Role.ADMIN))
    return client


def test_card_shows_the_fingerprint_and_forgetting_it_is_journaled(admin_client: Client) -> None:
    server = ServerFactory(host="203.0.113.10", port=22)
    KnownHost.objects.create(
        host="203.0.113.10", port=22, key_type="ssh-ed25519", fingerprint=FINGERPRINT
    )
    assert FINGERPRINT in admin_client.get(f"/servers/{server.pk}/").content.decode()

    response = admin_client.post(f"/servers/{server.pk}/host-key/forget/")

    assert response["Location"] == f"/servers/{server.pk}/"
    assert not KnownHost.objects.exists()
    event = AuditEvent.objects.get(action=FORGET_HOST_KEY)
    assert event.data == {"key_type": "ssh-ed25519", "fingerprint": FINGERPRINT}
    assert Operation.objects.filter(kind=CHECK_KIND).count() == 1


def test_forgetting_keeps_the_keys_of_other_servers(admin_client: Client) -> None:
    server = ServerFactory(host="203.0.113.10", port=22)
    KnownHost.objects.create(host="203.0.113.10", port=2222, key_type="ssh-rsa", fingerprint="a")

    admin_client.post(f"/servers/{server.pk}/host-key/forget/")

    assert KnownHost.objects.count() == 1
    assert not AuditEvent.objects.exists()
    assert not Operation.objects.exists()


def test_a_guest_cannot_forget_the_fingerprint(client: Client) -> None:
    server = ServerFactory(host="203.0.113.10", port=22)
    KnownHost.objects.create(host="203.0.113.10", port=22, key_type="ssh-rsa", fingerprint="a")
    client.force_login(UserFactory())

    client.post(f"/servers/{server.pk}/host-key/forget/")

    assert KnownHost.objects.exists()


def edit(admin_client: Client, server_pk: int, **fields: object) -> None:
    data = {
        "name": "Amsterdam",
        "host": "203.0.113.10",
        "port": 22,
        "username": "root",
        "auth_method": "key",
        "private_key": "",
        "passphrase": "",
        "password": "",
        "location": "",
        "paid_until": "",
        **fields,
    }
    response = admin_client.post(f"/servers/{server_pk}/edit/", data)
    assert response.status_code == 302


def test_a_new_address_forgets_the_fingerprint_of_the_old_one(admin_client: Client) -> None:
    server = ServerFactory(host="203.0.113.10", port=22)
    KnownHost.objects.create(host="203.0.113.10", port=22, key_type="ssh-rsa", fingerprint="a")
    KnownHost.objects.create(host="198.51.100.9", port=22, key_type="ssh-rsa", fingerprint="b")

    edit(admin_client, server.pk, host="198.51.100.9")

    assert list(KnownHost.objects.values_list("fingerprint", flat=True)) == ["b"]
    event = AuditEvent.objects.get(action=FORGET_HOST_KEY)
    assert event.data == {
        "key_type": "ssh-rsa",
        "fingerprint": "a",
        "host": "203.0.113.10",
        "port": 22,
    }


def test_a_new_port_forgets_the_fingerprint_too(admin_client: Client) -> None:
    server = ServerFactory(host="203.0.113.10", port=22)
    KnownHost.objects.create(host="203.0.113.10", port=22, key_type="ssh-rsa", fingerprint="a")

    edit(admin_client, server.pk, port=2222)

    assert not KnownHost.objects.exists()


def test_editing_other_fields_keeps_the_fingerprint(admin_client: Client) -> None:
    server = ServerFactory(host="203.0.113.10", port=22)
    KnownHost.objects.create(host="203.0.113.10", port=22, key_type="ssh-rsa", fingerprint="a")

    edit(admin_client, server.pk, name="Paris")

    assert KnownHost.objects.count() == 1
