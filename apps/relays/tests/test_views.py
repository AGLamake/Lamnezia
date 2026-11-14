"""The relay card on the server page and its buttons; what the relay sees on the group page."""

from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.groups.models import GroupMember, ServerGroup
from apps.operations.models import Operation
from apps.relays.models import Relay
from apps.relays.services.deployment import INSTALL_KIND
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db

AT = 1731580000


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


def health_of(group: ServerGroup, active: GroupMember, down: GroupMember) -> dict[str, Any]:
    def member(of: GroupMember) -> dict[str, Any]:
        return {"id": of.pk, "name": of.instance.server.name}

    reading = {
        "port": group.port,
        "active": member(active),
        "switched_at": AT - 30,
        "alive": [member(active)],
        "down": [member(down)],
    }
    return {"updated_at": AT, "groups": [reading]}


@pytest.fixture
def watched(relayed: ServerGroup) -> Relay:
    leader, joiner = GroupMember.objects.get(leading=True), GroupMember.objects.get(leading=False)
    assert relayed.relay is not None
    return Relay.objects.create(
        server=relayed.relay,
        installed_at=timezone.now(),
        health=health_of(relayed, active=joiner, down=leader),
        read_at=timezone.now(),
        rate_from_clients=2048.0,
        rate_to_clients=1024.0,
    )


def test_the_group_page_shows_what_the_relay_sees(browser: Client, watched: Relay) -> None:
    group = watched.server.relayed_groups.get()

    page = browser.get(reverse("groups:detail", args=[group.pk])).content.decode()

    assert "badge-info" in page  # the active member
    assert "badge-danger" in page  # the member the relay cannot reach
    assert "Last switch" in page


def test_the_relay_card_shows_the_load_and_the_active_member(
    browser: Client, watched: Relay
) -> None:
    joiner = GroupMember.objects.get(leading=False)

    page = browser.get(reverse("servers:detail", args=[watched.server.pk])).content.decode()

    assert "2.0\xa0KB/s" in page
    assert f"traffic goes to {joiner.instance.server.name}" in page


def test_the_relay_card_tells_when_the_state_is_not_read(browser: Client, watched: Relay) -> None:
    watched.read_error = "stale"
    watched.save(update_fields=["read_error"])

    page = browser.get(reverse("servers:detail", args=[watched.server.pk])).content.decode()

    assert "cannot read the relay state" in page
