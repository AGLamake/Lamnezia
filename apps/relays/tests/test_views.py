"""The relay card on the server page and its buttons."""

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.groups.models import ServerGroup
from apps.operations.models import Operation
from apps.relays.models import Relay
from apps.relays.services.deployment import INSTALL_KIND
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def browser(client: Client, admin: User) -> Client:
    client.force_login(admin)
    return client


def test_the_server_page_shows_the_groups_of_the_relay(browser: Client, group: ServerGroup) -> None:
    server = ServerFactory()
    group.relay = server
    group.save(update_fields=["relay"])

    page = browser.get(reverse("servers:detail", args=[server.pk])).content.decode()

    assert group.name in page
    assert reverse("relays:install", args=[server.pk]) in page
    assert reverse("relays:remove", args=[server.pk]) not in page


def test_install_starts_the_operation(browser: Client) -> None:
    server = ServerFactory()

    response = browser.post(reverse("relays:install", args=[server.pk]))

    operation = Operation.objects.get(kind=INSTALL_KIND)
    assert response.url == reverse("operations:detail", args=[operation.pk])  # type: ignore[attr-defined]
    assert Relay.objects.filter(server=server).exists()


def test_update_without_a_relay_returns_to_the_server(browser: Client) -> None:
    server = ServerFactory()

    response = browser.post(reverse("relays:update", args=[server.pk]))

    assert response.url == reverse("servers:detail", args=[server.pk])  # type: ignore[attr-defined]
    assert not Operation.objects.exists()


def test_a_moderator_cannot_install(client: Client) -> None:
    client.force_login(UserFactory(role=Role.MODERATOR))
    server: Server = ServerFactory()

    response = client.post(reverse("relays:install", args=[server.pk]))

    assert response.status_code == 403
    assert not Relay.objects.exists()
