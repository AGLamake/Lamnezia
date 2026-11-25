"""Every panel URL against every level of its section: what opens, what is refused with 403."""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from django.test import Client
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from django.views.generic.edit import ProcessFormView

from apps.accounts.models import Role, User
from apps.accounts.permissions import AdminRequiredMixin, RoleRequiredMixin, SectionRequiredMixin
from apps.accounts.services.access import POLICIES, Grant, Level, Section
from apps.accounts.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

_DUMMY = {"int": 999999, "uuid": uuid.UUID(int=1)}
# Views every staff member opens, whatever the sections: they show only what the member may see
# (the showcase of the interface exists only with DEBUG; API tokens are each member's own).
_FOR_EVERY_MEMBER = {
    "dashboard:search",
    "core:showcase",
    "api-tokens:list",
    "api-tokens:deactivate",
}


@dataclass(frozen=True)
class PanelUrl:
    name: str
    url: str
    view: type

    def __str__(self) -> str:
        return self.name


def _walk(patterns: list[URLPattern | URLResolver], prefix: str = "") -> Iterator[PanelUrl]:
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            namespace = f"{prefix}{pattern.namespace}:" if pattern.namespace else prefix
            yield from _walk(pattern.url_patterns, namespace)
            continue
        view = getattr(pattern.callback, "view_class", None)
        if view is None or not pattern.name or not _is_staff_only(view):
            continue
        name = prefix + pattern.name
        kwargs = {
            key: _DUMMY.get(type(converter).__name__.removesuffix("Converter").lower(), "x")
            for key, converter in pattern.pattern.converters.items()
        }
        yield PanelUrl(name, reverse(name, kwargs=kwargs), view)


def _is_staff_only(view: type) -> bool:
    return issubclass(view, RoleRequiredMixin) and Role.USER not in view.roles


PANEL_URLS = sorted(_walk(get_resolver().url_patterns), key=str)
SECTION_URLS = [url for url in PANEL_URLS if issubclass(url.view, SectionRequiredMixin)]
ADMIN_URLS = [url for url in PANEL_URLS if issubclass(url.view, AdminRequiredMixin)]


@pytest.fixture
def member(monkeypatch: pytest.MonkeyPatch) -> User:
    """A staff member whose role may get anything and has nothing until it is given."""
    open_role = {section: Grant(Level.NONE, Level.MANAGE) for section in Section.values}
    monkeypatch.setitem(POLICIES, Role.MODERATOR, open_role)
    return UserFactory(role=Role.MODERATOR)


def _status(member: User, method: str, url: str, access: dict[str, str]) -> int:
    member.access = access
    member.save(update_fields=["access"])
    client = Client(raise_request_exception=False)
    client.force_login(member)
    status: int = getattr(client, method)(url).status_code
    return status


def _needed(url: PanelUrl, method: str) -> Level:
    view = url.view
    if method == "post" or issubclass(view, ProcessFormView):
        return view.write_level  # type: ignore[attr-defined, no-any-return]
    return view.read_level  # type: ignore[attr-defined, no-any-return]


def test_every_panel_view_names_its_section_or_is_an_administrators() -> None:
    loose = {
        url.name
        for url in PANEL_URLS
        if not issubclass(url.view, SectionRequiredMixin | AdminRequiredMixin)
    }
    assert loose <= _FOR_EVERY_MEMBER


@pytest.mark.parametrize("url", SECTION_URLS, ids=str)
@pytest.mark.parametrize("method", ["get", "post"])
def test_url_opens_by_the_level_of_its_section(member: User, url: PanelUrl, method: str) -> None:
    section = url.view.section  # type: ignore[attr-defined]
    needed = _needed(url, method)
    for level in Level:
        status = _status(member, method, url.url, {section: level.name.lower()})
        assert (status == 403) == (level < needed), (level, status)


@pytest.mark.parametrize("url", SECTION_URLS, ids=str)
def test_other_sections_open_nothing(member: User, url: PanelUrl) -> None:
    section = url.view.section  # type: ignore[attr-defined]
    others = {other: "manage" for other in Section.values if other != section}
    assert _status(member, "get", url.url, others) == 403
    assert _status(member, "post", url.url, others) == 403


@pytest.mark.parametrize("url", ADMIN_URLS, ids=str)
def test_administrator_pages_stay_closed_to_every_section(member: User, url: PanelUrl) -> None:
    everything = {section: "manage" for section in Section.values}
    assert _status(member, "get", url.url, everything) == 403
    assert _status(member, "post", url.url, everything) == 403
