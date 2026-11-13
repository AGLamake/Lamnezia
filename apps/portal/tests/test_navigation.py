import pytest
from django.test import Client

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.guides.models import FaqArticle
from apps.guides.services.releases import LatestReleaseProvider
from apps.portal.tests.test_portal import subscriber

pytestmark = pytest.mark.django_db

PORTAL_TABS = ("/portal/", "/portal/servers/", "/support/", "/guides/")


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LatestReleaseProvider, "latest", lambda self: None)


@pytest.fixture
def user_client(client: Client) -> Client:
    client.force_login(subscriber())
    return client


@pytest.mark.parametrize(
    "path",
    [
        "/portal/",
        "/portal/servers/",
        "/support/",
        "/guides/",
        "/guides/android/",
        "/guides/faq/",
        "/guides/faq/vpn/",
        "/guides/materials/",
        "/guides/suggest/",
    ],
)
def test_every_portal_section_shows_all_portal_tabs(user_client: Client, path: str) -> None:
    FaqArticle.objects.create(slug="vpn", title_en="VPN", body_en="Text")

    page = user_client.get(path).content.decode()

    for tab in PORTAL_TABS:
        assert f'href="{tab}" class="nav-link"' in page, tab


def test_guides_mark_their_tab_and_the_section_inside(user_client: Client) -> None:
    page = user_client.get("/guides/faq/").content.decode()

    assert 'href="/guides/" class="nav-link" aria-current="page"' in page
    assert 'href="/guides/faq/" class="tab" aria-current="page"' in page
    assert 'href="/portal/" class="nav-link" aria-current' not in page


def test_a_guest_reads_the_guides_and_can_sign_in(client: Client) -> None:
    page = client.get("/guides/").content.decode()

    assert 'class="nav-link"' not in page
    assert "/accounts/login/?next=/guides/" in page


@pytest.mark.parametrize("role", [Role.ADMIN, Role.MODERATOR])
def test_the_team_switches_between_the_admin_and_the_portal(client: Client, role: Role) -> None:
    client.force_login(UserFactory(role=role))

    assert 'href="/dashboard/"' in client.get("/portal/").content.decode()
    assert 'href="/portal/" class="menu-item"' in client.get("/dashboard/").content.decode()


def test_a_client_has_no_way_into_the_admin(user_client: Client) -> None:
    assert 'href="/dashboard/"' not in user_client.get("/portal/").content.decode()
