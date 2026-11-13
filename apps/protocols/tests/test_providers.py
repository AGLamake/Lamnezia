from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.backups.tests.helpers import access_key, awg_instance
from apps.keys.services.instances import instances
from apps.servers.models import Server
from apps.servers.tests.factories import PRIVATE_KEY, ServerFactory
from apps.ssh.encryption import SecretCipher

pytestmark = pytest.mark.django_db

KEY_PAGES = ("keys:list", "keys:issue", "keys:drift")


def instance_with_a_key() -> str:
    instance = awg_instance()
    access_key(instance, "iPhone", "10.9.8.2")
    return instance.source


def test_titles_and_lookups_decrypt_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    source = instance_with_a_key()
    decrypted: list[str] = []
    real = SecretCipher.decrypt

    def decrypt(self: SecretCipher, token: str) -> str:
        decrypted.append(token)
        return real(self, token)

    monkeypatch.setattr(SecretCipher, "decrypt", decrypt)

    assert source in instances.titles()
    assert instances.get(source).source == source
    assert decrypted == []


def test_a_connection_still_gets_the_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    source = instance_with_a_key()
    seen: list[str] = []

    @contextmanager
    def connect(server: Server) -> Iterator[None]:
        seen.append(server.private_key)
        yield

    monkeypatch.setattr("apps.protocols.providers.connect", connect)
    with instances.get(source).connect():
        pass

    assert seen == [PRIVATE_KEY]


def test_key_pages_take_the_same_queries_for_any_number_of_instances(client: Client) -> None:
    client.force_login(UserFactory(role=Role.ADMIN))

    def queries() -> list[int]:
        counts = []
        for page in KEY_PAGES:
            with CaptureQueriesContext(connection) as captured:
                assert client.get(reverse(page)).status_code == 200
            counts.append(len(captured))
        return counts

    instance_with_a_key()
    queries()  # the first visit also loads what is cached afterwards
    one = queries()
    for _ in range(3):
        instance_with_a_key()

    assert queries() == one


def test_a_server_under_maintenance_is_not_offered_for_new_keys(client: Client) -> None:
    open_source = awg_instance().source
    closed = awg_instance(ServerFactory(maintenance=True))
    client.force_login(UserFactory(role=Role.ADMIN))

    choices = dict(client.get(reverse("keys:issue")).context["form"].fields["source"].choices)
    servers = client.get(reverse("servers:list")).content.decode()

    assert instances.get(closed.source).maintenance
    assert open_source in choices and closed.source not in choices
    assert "maintenance" in servers
