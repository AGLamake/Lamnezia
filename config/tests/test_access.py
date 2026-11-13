"""Every page of the panel, walked as a guest and as a user: nothing for staff leaks out."""

import re

import pytest
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver

from apps.accounts.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

# Pages a guest may open; every other page sends them to the login page.
PUBLIC = {
    "home",
    "set_language",
    "pwa-manifest",
    "pwa-worker",
    "pwa-offline",
    "accounts:login",
    "accounts:logout",
    "accounts:password-reset",
    "accounts:password-reset-sent",
    "accounts:password-reset-confirm",
    "accounts:register",
    "accounts:invite",
    "accounts:confirm-email",
    "accounts:magic-link",
    "accounts:magic-login",
    # Signing in with a linked messenger (404 unless an administrator turns it on).
    "accounts:telegram-login",
    "accounts:discord-login",
    "accounts:discord-callback",
    # Install guides, the FAQ and useful materials are for visitors who have no account yet.
    "guides:index",
    "guides:faq",
    "guides:faq-article",
    "guides:materials",
    "guides:platform",
    # Payment services report payments here; the provider checks who is speaking.
    "billing:notify",
    # XRay clients fetch the subscription link without signing in; the token is the access.
    "subscription-feed",
}
# The admin interface of the panel: a user with the "user" role gets 403 there.
STAFF_NAMESPACES = {
    "audit",
    "backups",
    "core",
    "dashboard",
    "keys",
    "notifications",
    "operations",
    "plans",
    "protocols",
    "servers",
}
STAFF_PREFIXES = (
    "accounts:registration",
    "accounts:invite-",
    "guides:moderation",
    "support:inbox",
    "support:incident",
)
# The Django admin site, health and metrics have their own protection and tests.
SKIPPED_ROUTES = ("admin/", "healthz", "metrics", "dev/")
_ARGUMENT = re.compile(r"<(?:(\w+):)?\w+>")
_SAMPLES = {"int": "1", "uuid": "00000000-0000-0000-0000-000000000001"}


def panel_pages() -> list[tuple[str, str]]:
    """(name, url) of every named page, with sample values for its arguments."""
    pages: list[tuple[str, str]] = []

    def walk(patterns: list[URLPattern | URLResolver], prefix: str, namespace: str) -> None:
        for pattern in patterns:
            route = prefix + str(pattern.pattern)
            if isinstance(pattern, URLResolver):
                walk(pattern.url_patterns, route, pattern.namespace or namespace)
            elif pattern.name and not route.startswith(SKIPPED_ROUTES):
                name = f"{namespace}:{pattern.name}" if namespace else pattern.name
                url = "/" + _ARGUMENT.sub(lambda m: _SAMPLES.get(m.group(1), "x"), route)
                pages.append((name, url))

    walk(get_resolver().url_patterns, "", "")
    return pages


def is_staff_page(name: str) -> bool:
    return name.split(":", 1)[0] in STAFF_NAMESPACES or name.startswith(STAFF_PREFIXES)


PAGES = panel_pages()
PRIVATE_PAGES = [page for page in PAGES if page[0] not in PUBLIC]
STAFF_PAGES = [page for page in PAGES if is_staff_page(page[0])]


@pytest.mark.parametrize(("name", "url"), PRIVATE_PAGES)
@pytest.mark.parametrize("method", ["get", "post"])
def test_a_guest_is_sent_to_the_login_page(
    client: Client, name: str, url: str, method: str
) -> None:
    response = getattr(client, method)(url)

    assert response.status_code == 302, name
    assert response["Location"].startswith("/accounts/login/"), name


@pytest.mark.parametrize(("name", "url"), STAFF_PAGES)
@pytest.mark.parametrize("method", ["get", "post"])
def test_a_user_gets_no_admin_page(client: Client, name: str, url: str, method: str) -> None:
    client.force_login(UserFactory(role="user"))

    response = getattr(client, method)(url)

    assert response.status_code == 403, name


def test_the_walk_finds_the_pages() -> None:
    assert len(STAFF_PAGES) > 50
    assert len(PRIVATE_PAGES) > len(STAFF_PAGES)
