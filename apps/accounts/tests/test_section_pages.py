"""Pages by section and level on real objects: "view" changes nothing and shows no action, the
menu, the tabs and Ctrl+K lead only where the member may go, the moderator keeps 1.0.0."""

from collections.abc import Iterator
from contextlib import AbstractContextManager

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Role, Status, User
from apps.accounts.tests.factories import UserFactory
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.instances import Instance, instances
from apps.keys.tests.conftest import HOST, SOURCE
from apps.keys.tests.test_views import issued_key
from apps.operations.models import Operation
from apps.plans.models import Plan
from apps.plans.tests.factories import PlanFactory
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db


def signed_in(client: Client, **access: str) -> User:
    moderator = UserFactory(role=Role.MODERATOR, access=access)
    client.force_login(moderator)
    return moderator


def _no_host() -> AbstractContextManager[RemoteHost]:
    raise AssertionError("pages of the panel do not connect to the server")


@pytest.fixture
def server() -> Iterator[Server]:
    """A server with the instance of the test keys on it."""
    server = ServerFactory()

    def provider() -> list[Instance]:
        return [Instance(SOURCE, "Amsterdam", HOST, _no_host, server_id=server.pk)]

    instances.register(provider)
    yield server
    instances.unregister(provider)


def owned_key(moderator: User, server: Server) -> AccessKey:
    """A key in the moderator's zone: their user's, on their server."""
    moderator.moderated_servers.add(server)
    return issued_key(user=UserFactory(managers=[moderator]))


def test_key_action_on_view_is_refused_and_starts_nothing(client: Client, server: Server) -> None:
    key = owned_key(signed_in(client, keys="view"), server)

    assert client.post(f"/keys/{key.pk}/disable/").status_code == 403

    key.refresh_from_db()
    assert key.status == KeyStatus.ACTIVE
    assert not Operation.objects.exists()


def test_server_check_on_view_is_refused_and_starts_nothing(client: Client) -> None:
    signed_in(client, servers="view")
    server = ServerFactory()

    assert client.post(f"/servers/{server.pk}/check/").status_code == 403
    assert not Operation.objects.exists()


def test_plan_delete_on_view_is_refused(client: Client) -> None:
    signed_in(client, plans="view")
    plan = PlanFactory()

    assert client.post(f"/plans/{plan.pk}/delete/").status_code == 403
    assert Plan.objects.filter(pk=plan.pk).exists()


def test_key_page_on_view_has_no_secret_and_no_action(client: Client, server: Server) -> None:
    key = owned_key(signed_in(client, keys="view"), server)

    html = client.get(f"/keys/{key.pk}/").content.decode()

    assert key.name in html
    assert f"/keys/{key.pk}/show/" not in html
    assert f"/keys/{key.pk}/disable/" not in html
    assert f"/keys/{key.pk}/assign/" not in html
    assert client.get(f"/keys/{key.pk}/show/").status_code == 403
    assert client.get(f"/keys/{key.pk}/file/conf/").status_code == 403


def test_key_page_on_manage_has_the_actions(client: Client, server: Server) -> None:
    key = owned_key(signed_in(client), server)

    html = client.get(f"/keys/{key.pk}/").content.decode()

    assert f"/keys/{key.pk}/show/" in html
    assert f"/keys/{key.pk}/disable/" in html


def links(client: Client, page: str, *names: str) -> dict[str, bool]:
    """Whether the page at `page` links to each of the URLs `names`."""
    html = client.get(reverse(page)).content.decode()
    return {name: f'href="{reverse(name)}"' in html for name in names}


MENU = (
    "dashboard:index",
    "dashboard:stats",
    "servers:list",
    "operations:list",
    "dashboard:users",
    "keys:list",
    "accounts:registrations",
    "plans:list",
    "billing:orders",
    "support:inbox",
    "audit:list",
    "core:settings",
)
MODERATOR_1_0_0 = {
    "dashboard:index",
    "dashboard:stats",
    "operations:list",
    "dashboard:users",
    "keys:list",
    "accounts:registrations",
    "support:inbox",
}


def test_menu_of_a_moderator_with_defaults_is_that_of_1_0_0(client: Client) -> None:
    signed_in(client)

    shown = links(client, "keys:list", *MENU)

    assert {name for name, linked in shown.items() if linked} == MODERATOR_1_0_0


def test_menu_leaves_out_the_sections_taken_away(client: Client) -> None:
    signed_in(client, stats="none", inbox="none", users="none")

    shown = links(client, "keys:list", "dashboard:stats", "support:inbox", "dashboard:users")

    assert not any(shown.values())


def test_menu_shows_a_section_given_for_viewing_without_its_actions(client: Client) -> None:
    signed_in(client, servers="view", plans="view")

    shown = links(client, "servers:list", "servers:list", "plans:list", "servers:add")

    assert shown == {"servers:list": True, "plans:list": True, "servers:add": False}


def test_start_page_is_the_first_section_the_member_opens(client: Client) -> None:
    signed_in(client, overview="none", stats="none")

    assert client.get(reverse("home")).headers["Location"] == reverse("operations:list")


def test_inbox_tabs_follow_the_sections(client: Client) -> None:
    signed_in(client)
    tabs = ("support:incidents", "support:templates")
    assert links(client, "support:inbox", *tabs) == dict.fromkeys(tabs, False)

    client.logout()
    signed_in(client, servers="view")
    assert links(client, "support:inbox", "support:incidents") == {"support:incidents": True}


def test_quick_search_finds_only_in_the_open_sections(client: Client) -> None:
    moderator = signed_in(client, keys="none")
    owner = UserFactory(username="findme-owner", managers=[moderator])
    issued_key(user=owner, name="findme-key")
    ServerFactory(name="findme-server")

    html = client.get(reverse("dashboard:search"), {"q": "findme"}).content.decode()

    assert "findme-owner" in html
    assert "findme-key" not in html
    assert "findme-server" not in html


def test_quick_search_shows_servers_of_the_zone_given_for_viewing(client: Client) -> None:
    moderator = signed_in(client, servers="view")
    mine = ServerFactory(name="findme-mine")
    moderator.moderated_servers.add(mine)
    ServerFactory(name="findme-other")

    html = client.get(reverse("dashboard:search"), {"q": "findme"}).content.decode()

    assert "findme-mine" in html
    assert "findme-other" not in html


def test_moderator_still_approves_nothing_of_the_registrations(client: Client) -> None:
    # In 1.0.0 applications were an administrator's; they wait for no staff member yet.
    signed_in(client)
    applicant = UserFactory(status=Status.PENDING)

    reject = reverse("accounts:registration-reject", args=[applicant.pk])
    assert client.get(reverse("accounts:registrations")).status_code == 200
    assert client.post(reject).status_code == 404
    applicant.refresh_from_db()
    assert applicant.status == Status.PENDING
