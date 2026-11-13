from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from apps.servers.models import AuthMethod
from apps.servers.tests.factories import PRIVATE_KEY, ServerFactory

pytestmark = pytest.mark.django_db


def test_credentials_carry_only_the_secrets_of_the_login_method() -> None:
    by_key = ServerFactory(host="vpn.example.com", port=2222, passphrase="phrase")
    by_password = ServerFactory(auth_method=AuthMethod.PASSWORD, password="pw", private_key="")

    key_creds, password_creds = by_key.credentials(), by_password.credentials()

    assert (key_creds.host, key_creds.port, key_creds.username) == ("vpn.example.com", 2222, "root")
    assert (key_creds.private_key, key_creds.passphrase, key_creds.password) == (
        PRIVATE_KEY,
        "phrase",
        "",
    )
    assert (password_creds.password, password_creds.private_key) == ("pw", "")


def test_secrets_are_encrypted_in_the_database() -> None:
    server = ServerFactory(password="plain-password", auth_method=AuthMethod.PASSWORD)

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT password, private_key FROM servers_server WHERE id = %s", [server.pk]
        )
        stored = " ".join(cursor.fetchone())

    assert "plain-password" not in stored
    assert "secret-key-body" not in stored


@pytest.mark.parametrize(("offset", "until", "overdue"), [(10, 10, 0), (0, 0, 0), (-3, -3, 3)])
def test_days_until_the_vps_is_paid(offset: int, until: int, overdue: int) -> None:
    server = ServerFactory(paid_until=timezone.localdate() + timedelta(days=offset))

    assert (server.days_until_paid, server.days_overdue) == (until, overdue)


def test_no_payment_date_means_no_days() -> None:
    server = ServerFactory(paid_until=None)

    assert (server.days_until_paid, server.days_overdue) == (None, 0)
