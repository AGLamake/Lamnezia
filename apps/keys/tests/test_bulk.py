import pytest
from django.contrib.messages import get_messages
from django.test import Client

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.keys.models import KeyStatus
from apps.keys.tests.test_views import issued_key
from apps.operations.models import Operation

pytestmark = pytest.mark.django_db


def texts(response: object) -> list[str]:
    return [str(message) for message in get_messages(response.wsgi_request)]  # type: ignore[attr-defined]


def test_list_offers_bulk_actions(admin_client: Client) -> None:
    key = issued_key()

    html = admin_client.get("/keys/").content.decode()

    assert 'action="/keys/bulk/"' in html
    assert f'name="keys" value="{key.pk}"' in html


def test_bulk_disable_starts_one_operation_per_key(admin_client: Client) -> None:
    first, second = issued_key(), issued_key(name="laptop", client_ip="10.8.1.6")

    response = admin_client.post(
        "/keys/bulk/", {"action": "disable", "keys": [first.pk, second.pk]}
    )

    operations = Operation.objects.filter(kind="keys.disable")
    assert sorted(op.params["key_id"] for op in operations) == sorted([first.pk, second.pk])
    assert response.headers["Location"] == "/operations/"
    assert texts(response) == ["2 operations started."]


def test_keys_in_a_wrong_status_are_skipped(admin_client: Client) -> None:
    active = issued_key()
    revoked = issued_key(name="old", client_ip="10.8.1.7", status=KeyStatus.REVOKED)

    response = admin_client.post(
        "/keys/bulk/", {"action": "enable", "keys": [active.pk, revoked.pk]}
    )

    assert not Operation.objects.exists()
    assert response.headers["Location"] == "/keys/"
    assert texts(response) == ["2 keys were skipped: their status does not allow it."]


def test_keys_outside_the_zone_are_refused(client: Client) -> None:
    moderator = UserFactory(role=Role.MODERATOR)
    stranger_key = issued_key(user=UserFactory())
    client.force_login(moderator)

    response = client.post("/keys/bulk/", {"action": "revoke", "keys": [stranger_key.pk]})

    assert not Operation.objects.exists()
    assert response.headers["Location"] == "/keys/"
    assert texts(response) == ["Tick the keys and choose what to do with them."]
