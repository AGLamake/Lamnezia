"""The relay card on the server page and its buttons; what the relay sees on the group page."""

from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from amnezia_core.relay import DEFAULT_FAILURES
from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.groups.models import GroupMember, ServerGroup
from apps.operations.models import Operation, OperationStatus
from apps.relays.models import Relay, SyncSkip
from apps.relays.services.deployment import INSTALL_KIND, ROUTING_KIND, UPDATE_KIND
from apps.relays.services.image import IMAGE_VERSION, agent_digest
from apps.relays.services.switch import SWITCH_KIND
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
        image_version=IMAGE_VERSION,
        agent_digest=agent_digest(),
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


def test_clients_without_an_answer_are_shown_on_both_pages(browser: Client, watched: Relay) -> None:
    group = watched.server.relayed_groups.get()
    detail = reverse("groups:detail", args=[group.pk])
    card = reverse("servers:detail", args=[watched.server.pk])
    assert "clients get no answer" not in browser.get(detail).content.decode()

    watched.health["groups"][0]["unanswered"] = True
    watched.save(update_fields=["health"])

    assert "clients get no answer" in browser.get(detail).content.decode()
    assert "the clients get no answer from it" in browser.get(card).content.decode()


def test_the_card_tells_why_the_sync_skips_the_relay(browser: Client, watched: Relay) -> None:
    watched.sync_skipped = SyncSkip.MEMBER_ON_RELAY
    watched.sync_skipped_detail = "Europe: AmneziaWG"
    watched.save(update_fields=["sync_skipped", "sync_skipped_detail"])

    page = browser.get(reverse("servers:detail", args=[watched.server.pk])).content.decode()

    assert "a member runs on the relay server" in page
    assert "Europe: AmneziaWG" in page


def test_saved_settings_are_written_by_an_update(browser: Client, watched: Relay) -> None:
    url = reverse("relays:settings", args=[watched.server.pk])

    response = browser.post(url, {"check_interval": "2", "failures": "4"})

    operation = Operation.objects.get(kind=UPDATE_KIND)
    assert response.url == reverse("operations:detail", args=[operation.pk])  # type: ignore[attr-defined]
    watched.refresh_from_db()
    assert (watched.check_interval, watched.failures) == (2, 4)


def test_wrong_settings_are_refused(browser: Client, watched: Relay) -> None:
    url = reverse("relays:settings", args=[watched.server.pk])

    browser.post(url, {"check_interval": "0", "failures": "4"})

    watched.refresh_from_db()
    assert watched.failures == DEFAULT_FAILURES
    assert not Operation.objects.filter(kind=UPDATE_KIND).exists()


def test_the_relay_card_tells_when_the_state_is_not_read(browser: Client, watched: Relay) -> None:
    watched.read_error = "stale"
    watched.save(update_fields=["read_error"])

    page = browser.get(reverse("servers:detail", args=[watched.server.pk])).content.decode()

    assert "cannot read the relay state" in page


def test_switch_is_offered_for_a_member_that_is_not_active(browser: Client, watched: Relay) -> None:
    group = watched.server.relayed_groups.get()
    leader, joiner = GroupMember.objects.get(leading=True), GroupMember.objects.get(leading=False)

    page = browser.get(reverse("groups:detail", args=[group.pk])).content.decode()

    assert reverse("groups:switch", args=[leader.pk]) in page
    assert reverse("groups:switch", args=[joiner.pk]) not in page, "the active one is not offered"
    assert "about 20 seconds" in page


def test_switch_starts_an_operation(browser: Client, watched: Relay) -> None:
    leader = GroupMember.objects.get(leading=True)

    response = browser.post(reverse("groups:switch", args=[leader.pk]))

    assert response.url == reverse("groups:detail", args=[leader.group_id])  # type: ignore[attr-defined]
    assert Operation.objects.get(kind=SWITCH_KIND).params["member_id"] == leader.pk


def test_switch_without_an_installed_relay_says_why(browser: Client, watched: Relay) -> None:
    Relay.objects.update(installed_at=None)
    leader = GroupMember.objects.get(leading=True)

    response = browser.post(reverse("groups:switch", args=[leader.pk]), follow=True)

    assert not Operation.objects.filter(kind=SWITCH_KIND).exists()
    assert "relay container is not on the server" in response.content.decode()


def test_the_group_page_shows_the_result_of_the_last_planned_switch(
    browser: Client, watched: Relay
) -> None:
    group = watched.server.relayed_groups.get()
    params = {"group_id": group.pk}
    Operation.objects.create(kind=SWITCH_KIND, params=params, status=OperationStatus.SUCCEEDED)
    failed = Operation.objects.create(
        kind=SWITCH_KIND,
        params=params,
        subject="Europe: AmneziaWG",
        status=OperationStatus.FAILED,
        error_message="The relay cannot reach the server.",
    )
    Operation.objects.create(kind=SWITCH_KIND, params={"group_id": group.pk + 1})

    page = browser.get(reverse("groups:detail", args=[group.pk])).content.decode()

    assert "Last planned switch" in page
    assert reverse("operations:detail", args=[failed.pk]) in page
    assert "The relay cannot reach the server." in page


def test_a_group_never_switched_by_hand_shows_no_planned_switch(
    browser: Client, watched: Relay
) -> None:
    group = watched.server.relayed_groups.get()

    page = browser.get(reverse("groups:detail", args=[group.pk])).content.decode()

    assert "Last planned switch" not in page


def test_the_group_page_offers_the_other_routing(browser: Client, watched: Relay) -> None:
    group = watched.server.relayed_groups.get()

    page = browser.get(reverse("groups:detail", args=[group.pk])).content.decode()

    assert reverse("groups:routing", args=[group.pk]) in page
    assert 'name="routing" value="split"' in page
    assert "one new handshake" in page


def test_routing_starts_an_operation(browser: Client, watched: Relay) -> None:
    group = watched.server.relayed_groups.get()

    response = browser.post(reverse("groups:routing", args=[group.pk]), {"routing": "split"})

    assert response.url == reverse("groups:detail", args=[group.pk])  # type: ignore[attr-defined]
    params = Operation.objects.get(kind=ROUTING_KIND).params
    assert (params["group_id"], params["routing"]) == (group.pk, "split")


def test_an_older_image_asks_for_a_reinstall(browser: Client, watched: Relay) -> None:
    group = watched.server.relayed_groups.get()
    Relay.objects.update(image_version=IMAGE_VERSION - 1)

    card = browser.get(reverse("servers:detail", args=[watched.server_id])).content.decode()
    page = browser.get(reverse("groups:detail", args=[group.pk])).content.decode()

    assert "needs reinstall" in card
    assert 'id="relay-reinstall"' in card
    assert reverse("relays:install", args=[watched.server_id]) in card
    assert 'id="split-refusal"' in page
    assert "older image" in page
    assert f"{reverse('servers:detail', args=[watched.server_id])}#relay" in page


def test_a_current_relay_offers_the_split(browser: Client, watched: Relay) -> None:
    group = watched.server.relayed_groups.get()

    card = browser.get(reverse("servers:detail", args=[watched.server_id])).content.decode()
    page = browser.get(reverse("groups:detail", args=[group.pk])).content.decode()

    assert "needs reinstall" not in card
    assert 'id="split-refusal"' not in page
    assert "Switch to split routing" in page


def test_a_split_on_an_older_agent_says_why(browser: Client, watched: Relay) -> None:
    group = watched.server.relayed_groups.get()
    Relay.objects.update(agent_digest="")

    url = reverse("groups:routing", args=[group.pk])
    response = browser.post(url, {"routing": "split"}, follow=True)

    assert not Operation.objects.filter(kind=ROUTING_KIND).exists()
    assert "agent is of an older release" in response.content.decode()


def test_the_same_routing_says_why(browser: Client, watched: Relay) -> None:
    group = watched.server.relayed_groups.get()

    url = reverse("groups:routing", args=[group.pk])
    response = browser.post(url, {"routing": "forward"}, follow=True)

    assert not Operation.objects.filter(kind=ROUTING_KIND).exists()
    assert "already has this routing" in response.content.decode()


def test_the_card_shows_settings_the_panel_did_not_write(browser: Client, watched: Relay) -> None:
    group = watched.server.relayed_groups.get()
    watched.health["groups"].append({"port": 5542, "active": None, "alive": [], "down": []})
    watched.mismatch = {"foreign_ports": [5542], "missing_ports": [group.port], "groups": []}
    watched.save(update_fields=["health", "mismatch"])

    page = browser.get(reverse("servers:detail", args=[watched.server.pk])).content.decode()

    assert "other settings than the panel wrote" in page
    assert "UDP 5542" in page
    assert "UDP 5542: no server reachable" not in page
