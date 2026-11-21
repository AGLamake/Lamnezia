"""Deleting a relay server: refused until its relay is removed, which moves the keys back."""

import json

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.groups.models import ServerGroup
from apps.groups.tests.conftest import JOINER_HOST, LEADER_HOST, PORT, Fakes, succeeded
from apps.protocols.models import ProtocolInstance
from apps.protocols.tests.awg_case import Start, issued_key
from apps.relays.models import Relay, ServicePeer
from apps.relays.services import deployment
from apps.relays.services.deployment import INSTALL_KIND, REMOVE_KIND
from apps.relays.tests.conftest import RELAY_HOST
from apps.relays.tests.fake_relay import FakeRelayServer
from apps.relays.tests.test_deployment import peers_of
from apps.servers.models import Server
from apps.servers.services.staff import ServerStaff, ServerStaffError
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db


def run(kind: str, server: Server, admin: User, start: Start) -> None:
    succeeded(start(lambda: deployment.start(kind, server, user=admin)))


def delete(server: Server, member: User) -> None:
    ServerStaff(Server.objects.get(pk=server.pk), member).delete()


def test_a_relay_server_is_kept_until_its_relay_is_removed(
    relayed: ServerGroup,
    leader: ProtocolInstance,
    relay_server: Server,
    admin: User,
    start: Start,
) -> None:
    key = issued_key(leader, UserFactory())
    run(INSTALL_KIND, relay_server, admin, start)

    with pytest.raises(ServerStaffError, match="remove the relay on its card first"):
        delete(relay_server, admin)
    assert Server.objects.filter(pk=relay_server.pk).exists()

    run(REMOVE_KIND, relay_server, admin, start)
    delete(relay_server, admin)

    key.refresh_from_db()
    profile = json.loads(key.profile)
    assert (profile["host"], profile["port"]) == (LEADER_HOST, PORT)
    assert not Server.objects.filter(pk=relay_server.pk).exists()


def test_a_relay_not_installed_yet_keeps_its_server_too(admin: User) -> None:
    """An install that failed may have put the service peers on the members already."""
    server = ServerFactory()
    Relay.objects.create(server=server)

    with pytest.raises(ServerStaffError):
        delete(server, admin)


def test_a_server_administrator_is_sent_to_an_administrator() -> None:
    owner = UserFactory(role=Role.OWNER)
    server = ServerFactory()
    server.moderators.add(owner)
    Relay.objects.create(server=server)

    with pytest.raises(ServerStaffError, match="ask an administrator"):
        delete(server, owner)


def test_the_server_page_tells_why_it_is_not_deleted(client: Client, admin: User) -> None:
    server = ServerFactory()
    Relay.objects.create(server=server)
    client.force_login(admin)

    response = client.post(reverse("servers:delete", args=[server.pk]), follow=True)

    assert "remove the relay on its card first" in response.content.decode()
    assert Server.objects.filter(pk=server.pk).exists()


@pytest.mark.usefixtures("relayed")
def test_a_relay_server_that_does_not_answer_is_removed_all_the_same(  # noqa: PLR0913, PLR0917
    leader: ProtocolInstance,
    relay_server: Server,
    relay_fake: FakeRelayServer,
    servers: Fakes,
    down: set[str],
    admin: User,
    start: Start,
) -> None:
    key = issued_key(leader, UserFactory())
    run(INSTALL_KIND, relay_server, admin, start)
    public_key = ServicePeer.objects.get().public_key
    down.add(RELAY_HOST)

    operation = start(lambda: deployment.start(REMOVE_KIND, relay_server, user=admin))

    succeeded(operation)
    logs = "".join(step.title + step.log for step in operation.steps.all())
    assert "Leave the container on the server that does not answer" in logs
    assert relay_fake.state, "the container stays on the server that does not answer"
    assert not Relay.objects.exists() and not ServicePeer.objects.exists()
    assert public_key not in peers_of(servers[LEADER_HOST]) | peers_of(servers[JOINER_HOST])
    key.refresh_from_db()
    assert json.loads(key.profile)["host"] == LEADER_HOST
    delete(relay_server, admin)
