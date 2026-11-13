import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.dashboard.services.palette import QuickSearch
from apps.keys.tests.test_views import issued_key
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db


def titles(member: User, text: str) -> dict[str, list[str]]:
    groups = QuickSearch(member).groups(text)
    return {group.title: [hit.title for hit in group.hits] for group in groups}


def test_admin_finds_servers_users_and_keys() -> None:
    ServerFactory(name="Ameland")
    amelia = UserFactory(username="amelia")
    issued_key(user=amelia, name="Amelphone")

    found = titles(UserFactory(role=Role.ADMIN), "amel")

    assert found == {"Servers": ["Ameland"], "Users": ["amelia"], "Keys": ["Amelphone"]}


def test_moderator_finds_only_their_users_and_no_servers() -> None:
    moderator = UserFactory(role=Role.MODERATOR)
    ServerFactory(name="Ameland")
    UserFactory(username="amelia", manager=moderator)
    UserFactory(username="amelie")

    assert titles(moderator, "amel") == {"Users": ["amelia"]}


def test_short_text_finds_nothing() -> None:
    UserFactory(username="a")

    assert QuickSearch(UserFactory(role=Role.ADMIN)).groups(" a ") == []


def test_results_page_lists_links(client: Client) -> None:
    client.force_login(UserFactory(role=Role.ADMIN))
    server = ServerFactory(name="Amsterdam")

    html = client.get(reverse("dashboard:search"), {"q": "amst"}).content.decode()

    assert reverse("servers:detail", args=[server.pk]) in html
    assert "data-palette-item" in html


def test_results_page_says_when_nothing_matches(client: Client) -> None:
    client.force_login(UserFactory(role=Role.ADMIN))

    html = client.get(reverse("dashboard:search"), {"q": "zzz"}).content.decode()

    assert "Nothing found" in html


def test_search_is_for_staff_only(client: Client) -> None:
    client.force_login(UserFactory(role=Role.USER))

    assert client.get(reverse("dashboard:search"), {"q": "ab"}).status_code == 403


def test_admin_pages_carry_the_palette(client: Client) -> None:
    client.force_login(UserFactory(role=Role.ADMIN))

    html = client.get(reverse("dashboard:index")).content.decode()

    assert 'id="palette"' in html
    assert 'data-dialog-open="palette"' in html
