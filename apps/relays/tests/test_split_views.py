"""The network list of a split group on the group page: its sources and own entries."""

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.groups.models import GroupRouting, ServerGroup
from apps.protocols.tests.awg_case import OnCommit
from apps.relays.models import SourceKind, SplitDirection, SplitEntry, SplitSource
from apps.relays.tests.conftest import FakeLists

pytestmark = pytest.mark.django_db

URL = "https://lists.example/ru.txt"


@pytest.fixture
def browser(client: Client, admin: User) -> Client:
    client.force_login(admin)
    return client


@pytest.fixture
def split() -> ServerGroup:
    return ServerGroup.objects.create(name="Moscow", port=40000, routing=GroupRouting.SPLIT)


def lists_url(group: ServerGroup) -> str:
    return reverse("groups:detail", args=[group.pk]) + "#split-lists"


def test_the_group_page_shows_the_list_of_a_split_group(
    browser: Client, split: ServerGroup
) -> None:
    SplitSource.objects.create(group=split, url=URL, kind=SourceKind.CIDR, error="offline")
    SplitEntry.objects.create(group=split, value="example.ru", direction=SplitDirection.ABROAD)

    page = browser.get(reverse("groups:detail", args=[split.pk])).content.decode()

    assert 'id="split-lists"' in page
    assert URL in page and "offline" in page and "example.ru" in page
    assert reverse("relays:source_add", args=[split.pk]) in page


def test_a_forwarded_group_shows_no_list(browser: Client, group: ServerGroup) -> None:
    page = browser.get(reverse("groups:detail", args=[group.pk])).content.decode()

    assert 'id="split-lists"' not in page


def test_a_new_source_is_downloaded_at_once(
    browser: Client,
    split: ServerGroup,
    lists: FakeLists,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    lists.bodies[URL] = b"5.8.0.0/16\n"

    with django_capture_on_commit_callbacks(execute=True):
        response = browser.post(
            reverse("relays:source_add", args=[split.pk]), {"url": URL, "kind": SourceKind.CIDR}
        )

    assert response.url == lists_url(split)  # type: ignore[attr-defined]
    source = SplitSource.objects.get()
    assert source.size == 1
    assert AuditEvent.objects.filter(action="relays.split_source_add").exists()


@pytest.mark.parametrize("url", ["ftp://lists.example/ru.txt", URL])
def test_a_bad_or_repeated_source_is_refused(browser: Client, split: ServerGroup, url: str) -> None:
    SplitSource.objects.create(group=split, url=URL, kind=SourceKind.CIDR)

    browser.post(reverse("relays:source_add", args=[split.pk]), {"url": url, "kind": "cidr"})

    assert SplitSource.objects.count() == 1


def test_a_source_is_turned_off_and_deleted(browser: Client, split: ServerGroup) -> None:
    source = SplitSource.objects.create(group=split, url=URL, kind=SourceKind.CIDR)

    browser.post(reverse("relays:source_toggle", args=[source.pk]))
    source.refresh_from_db()
    assert not source.enabled
    browser.post(reverse("relays:source_delete", args=[source.pk]))

    assert not SplitSource.objects.exists()


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("Пример.РФ", "xn--e1afmkfd.xn--p1ai"),
        ("192.0.2.7", "192.0.2.7/32"),
        ("10.1.2.3/8", "10.0.0.0/8"),
    ],
)
def test_an_own_entry_is_stored_as_the_agent_gets_it(
    browser: Client, split: ServerGroup, typed: str, stored: str
) -> None:
    browser.post(
        reverse("relays:entry_add", args=[split.pk]), {"value": typed, "direction": "local"}
    )

    assert SplitEntry.objects.get().value == stored


@pytest.mark.parametrize("typed", ["2001:db8::/32", "not a domain", "localhost", "example.ru"])
def test_a_bad_or_repeated_entry_is_refused(
    browser: Client, split: ServerGroup, typed: str
) -> None:
    SplitEntry.objects.create(group=split, value="example.ru", direction=SplitDirection.LOCAL)

    browser.post(
        reverse("relays:entry_add", args=[split.pk]), {"value": typed, "direction": "abroad"}
    )

    assert list(SplitEntry.objects.values_list("value", flat=True)) == ["example.ru"]


def test_an_own_entry_is_deleted(browser: Client, split: ServerGroup) -> None:
    entry = SplitEntry.objects.create(group=split, value="example.ru")

    browser.post(reverse("relays:entry_delete", args=[entry.pk]))

    assert not SplitEntry.objects.exists()
    assert AuditEvent.objects.filter(action="relays.split_entry_delete").exists()


def test_only_admins_change_the_list(client: Client, split: ServerGroup) -> None:
    client.force_login(UserFactory(role=Role.USER))

    client.post(reverse("relays:entry_add", args=[split.pk]), {"value": "example.ru"})

    assert not SplitEntry.objects.exists()
