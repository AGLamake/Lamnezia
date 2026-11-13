from io import StringIO
from typing import Any

import pytest
from cryptography.fernet import Fernet
from django.core.management import call_command
from django.db import connection

from apps.operations.models import Operation
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.encryption import SecretCipher

pytestmark = pytest.mark.django_db


def stored_password(server: Server) -> str:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT password FROM {Server._meta.db_table} WHERE id = %s", [server.pk])
        return str(cursor.fetchone()[0])


def test_rotation_re_encrypts_secrets_with_the_first_key(settings: Any) -> None:
    old, new = settings.ENCRYPTION_KEY, Fernet.generate_key().decode()
    server = ServerFactory(password="ssh-password")
    settings.ENCRYPTION_KEY = f"{new},{old}"
    out = StringIO()

    call_command("rotate_encryption_key", stdout=out)

    assert SecretCipher(new).decrypt(stored_password(server)) == "ssh-password"
    settings.ENCRYPTION_KEY = new
    assert Server.objects.get(pk=server.pk).password == "ssh-password"
    assert "re-encrypted" in out.getvalue()


def test_rotation_re_encrypts_secrets_in_operation_params(settings: Any) -> None:
    old, new = settings.ENCRYPTION_KEY, Fernet.generate_key().decode()
    token = SecretCipher(old).encrypt("socks-password")
    operation = Operation.objects.create(
        kind="protocols.change_service_settings",
        params={"port": 1080, "options": {"user": "u"}, "password_token": token},
    )
    plain = Operation.objects.create(kind="servers.check", params={"server_id": 1})
    settings.ENCRYPTION_KEY = f"{new},{old}"

    call_command("rotate_encryption_key", stdout=StringIO())

    operation.refresh_from_db()
    assert SecretCipher(new).decrypt(operation.params["password_token"]) == "socks-password"
    assert operation.params["options"] == {"user": "u"}
    plain.refresh_from_db()
    assert plain.params == {"server_id": 1}


def test_rotation_keeps_empty_and_missing_values(settings: Any) -> None:
    server = ServerFactory(password="", private_key="")
    settings.ENCRYPTION_KEY = f"{Fernet.generate_key().decode()},{settings.ENCRYPTION_KEY}"

    call_command("rotate_encryption_key", stdout=StringIO())

    server.refresh_from_db()
    assert (server.password, server.private_key) == ("", "")
