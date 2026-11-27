"""Network lists of split groups: the daily update of the sources and the files for the relay."""

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.groups.models import GroupListMode, GroupRouting, ServerGroup
from apps.groups.tests.conftest import succeeded
from apps.notifications.events import Event
from apps.protocols.tests.awg_case import OnCommit, Start
from apps.relays.models import (
    RIPE_DELEGATIONS_URL,
    Relay,
    SourceKind,
    SplitDirection,
    SplitEntry,
    SplitSource,
)
from apps.relays.receivers import SyncBatch
from apps.relays.services import deployment, split_lists
from apps.relays.services.container import FOLDER
from apps.relays.services.deployment import INSTALL_KIND
from apps.relays.services.routing import start_routing
from apps.relays.services.split_lists import GroupList, SourceUpdate
from apps.relays.tests.conftest import FakeLists
from apps.relays.tests.fake_relay import FakeRelayServer
from apps.servers.models import Server

pytestmark = pytest.mark.django_db

URL = "https://lists.example/ru.txt"
RIPE = (
    "2|ripencc|1700000000|3|19830705|20231114|+0100\n"
    "ripencc|RU|ipv4|5.8.0.0|65536|20100101|allocated\n"
    "ripencc|RU|ipv4|5.9.0.0|65536|20100101|allocated\n"
    "ripencc|DE|ipv4|5.10.0.0|65536|20100101|allocated\n"
)


class Emitted:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str]] = []

    def __call__(self, event: str, data: dict[str, Any], *, key: str) -> None:
        self.calls.append((event, data, key))


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> Emitted:
    fake = Emitted()
    monkeypatch.setattr(split_lists, "emit", fake)
    return fake


@pytest.fixture
def plain() -> ServerGroup:
    return ServerGroup.objects.create(name="Moscow", port=40000)


def source(group: ServerGroup, url: str = URL, **fields: Any) -> SplitSource:
    return SplitSource.objects.create(group=group, url=url, kind=SourceKind.CIDR, **fields)


def update(at: Any = None) -> SourceUpdate:
    update = SourceUpdate.from_settings(at or timezone.now())
    update.run(update.due())
    return update


def test_a_list_is_taken_merged(plain: ServerGroup, lists: FakeLists) -> None:
    lists.bodies[URL] = b"5.8.0.0/16\n5.9.0.0/16\n# comment\n10.0.0.0/8\n"
    found = source(plain)

    update()

    found.refresh_from_db()
    assert found.networks.split() == ["5.8.0.0/15", "10.0.0.0/8"]
    assert found.size == 2 and found.error == ""
    assert found.updated_at is not None and found.checked_at == found.updated_at


def test_the_ripe_delegations_give_the_russian_blocks(plain: ServerGroup, lists: FakeLists) -> None:
    lists.bodies[RIPE_DELEGATIONS_URL] = RIPE.encode()
    found = SplitSource.objects.create(group=plain, url=RIPE_DELEGATIONS_URL, kind=SourceKind.RIPE)

    update()

    found.refresh_from_db()
    assert found.networks.split() == ["5.8.0.0/15"]


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (None, "offline"),
        (b"", "no IPv4 network"),
        (b"5.8.0.0/16\nnonsense\n", "Line 2"),
        (b"5.8.0.0/16\n", "less than half of 3"),
    ],
)
def test_a_bad_list_keeps_the_old_one_and_tells_the_admins(
    plain: ServerGroup,
    lists: FakeLists,
    emitted: Emitted,
    body: bytes | None,
    error: str,
) -> None:
    old = "1.0.0.0/24\n2.0.0.0/24\n3.0.0.0/24\n"
    now = timezone.now()
    found = source(plain, networks=old, size=3, updated_at=now - timedelta(days=2, hours=1))
    if body is not None:
        lists.bodies[URL] = body

    update(now)

    found.refresh_from_db()
    assert found.networks == old and found.size == 3
    assert error in found.error and found.checked_at == now
    ((event, data, key),) = emitted.calls
    assert event == Event.SPLIT_LIST_STALE
    assert data == {"group": "Moscow", "source": "lists.example", "days": 3, "error": data["error"]}
    assert key == f"split_list_stale:{found.pk}:{now.date()}"


def test_a_source_is_downloaded_once_for_all_groups(lists: FakeLists, plain: ServerGroup) -> None:
    lists.bodies[URL] = b"5.8.0.0/16\n"
    other = ServerGroup.objects.create(name="Kazan", port=40001)
    source(plain)
    source(other)

    update()

    assert lists.asked == [URL]
    assert set(SplitSource.objects.values_list("size", flat=True)) == {1}


def test_only_due_sources_are_downloaded(plain: ServerGroup, lists: FakeLists) -> None:
    now = timezone.now()
    source(plain, "https://a.example/fresh", checked_at=now - timedelta(hours=23))
    source(plain, "https://a.example/old", checked_at=now - timedelta(days=1))
    source(plain, "https://a.example/new")
    source(plain, "https://a.example/failed", checked_at=now, error="offline")
    source(plain, "https://a.example/off", enabled=False)

    update(now)

    assert sorted(lists.asked) == [
        "https://a.example/failed",
        "https://a.example/new",
        "https://a.example/old",
    ]


def test_a_changed_list_syncs_the_relay(
    relayed: ServerGroup,
    relay_server: Server,
    lists: FakeLists,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    found = source(relayed)
    Relay.objects.create(server=relay_server, installed_at=timezone.now())
    lists.bodies[URL] = b"5.8.0.0/16\n"

    with django_capture_on_commit_callbacks() as changed:
        update()
    found.refresh_from_db()
    with django_capture_on_commit_callbacks() as same:
        update(timezone.now() + timedelta(days=1))

    assert [batch.servers for batch in changed if isinstance(batch, SyncBatch)] == [
        {relay_server.pk}
    ]
    assert not [batch for batch in same if isinstance(batch, SyncBatch)]


def test_the_group_list_merges_sources_and_own_entries(plain: ServerGroup) -> None:
    source(plain, networks="5.8.0.0/15\n")
    source(plain, "https://a.example/off", networks="6.0.0.0/8\n", enabled=False)
    for value, direction in [
        ("192.0.2.0/24", SplitDirection.LOCAL),
        ("5.8.1.0/24", SplitDirection.ABROAD),
        ("Example.RU", SplitDirection.LOCAL),
        ("video.example", SplitDirection.ABROAD),
    ]:
        SplitEntry.objects.create(group=plain, value=value, direction=direction)

    found = GroupList.of(plain)

    assert [str(network) for network in found.networks][:2] == ["5.8.0.0/24", "5.8.2.0/23"]
    assert "6.0.0.0/8" not in {str(network) for network in found.networks}
    assert found.files(40000) == {
        "networks-40000.txt": found.files(40000)["networks-40000.txt"],
        "domains-40000.txt": "local example.ru\nabroad video.example\n",
    }
    assert "192.0.2.0/24" in found.files(40000)["networks-40000.txt"]


def test_a_group_without_lists_has_no_files(plain: ServerGroup) -> None:
    assert GroupList.of(plain).files(40000) == {}


def test_own_entries_and_sources_sync_the_relay(
    relayed: ServerGroup, relay_server: Server, django_capture_on_commit_callbacks: OnCommit
) -> None:
    Relay.objects.create(server=relay_server, installed_at=timezone.now())

    with django_capture_on_commit_callbacks() as callbacks:
        entry = SplitEntry.objects.create(group=relayed, value="example.ru")
        entry.delete()
        source(relayed)

    (batch,) = [callback for callback in callbacks if isinstance(callback, SyncBatch)]
    assert batch.servers == {relay_server.pk}


def test_split_takes_the_default_list_and_gives_it_to_the_relay(  # noqa: PLR0913, PLR0917
    relayed: ServerGroup,
    relay_server: Server,
    relay_fake: FakeRelayServer,
    lists: FakeLists,
    admin: User,
    start: Start,
) -> None:
    succeeded(start(lambda: deployment.start(INSTALL_KIND, relay_server, user=admin)))
    lists.bodies[RIPE_DELEGATIONS_URL] = RIPE.encode()

    succeeded(start(lambda: start_routing(relayed, GroupRouting.SPLIT, user=admin, ip="")))

    (found,) = relayed.split_sources.all()
    assert found.kind == SourceKind.RIPE and found.size == 1
    assert relay_fake.files[f"{FOLDER}/networks-{relayed.port}.txt"].decode() == "5.8.0.0/15\n"


def test_a_second_split_keeps_the_sources(plain: ServerGroup) -> None:
    source(plain)

    assert split_lists.add_default_source(plain) is None
    assert (
        split_lists.add_default_source(ServerGroup.objects.create(name="Ufa", port=1)) is not None
    )


def test_a_whitelist_takes_no_default_list() -> None:
    group = ServerGroup.objects.create(name="Ufa", port=1, list_mode=GroupListMode.INCLUDE)

    assert split_lists.add_default_source(group) is None


def test_the_whitelist_takes_the_lists_but_the_local_entries(plain: ServerGroup) -> None:
    plain.list_mode = GroupListMode.INCLUDE
    plain.save()
    source(plain, networks="5.8.0.0/23\n")
    for value, direction in [
        ("5.8.0.0/24", SplitDirection.LOCAL),
        ("192.0.2.0/24", SplitDirection.ABROAD),
    ]:
        SplitEntry.objects.create(group=plain, value=value, direction=direction)

    found = GroupList.of(plain)

    assert [str(network) for network in found.networks] == ["5.8.1.0/24", "192.0.2.0/24"]


def test_the_mode_change_syncs_the_relay_and_turns_off_the_lists(
    relayed: ServerGroup,
    relay_server: Server,
    admin: User,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    source(relayed)
    Relay.objects.create(server=relay_server, installed_at=timezone.now())

    with django_capture_on_commit_callbacks() as callbacks:
        turned_off = split_lists.change_list_mode(relayed, GroupListMode.INCLUDE, user=admin)

    assert turned_off == 1
    relayed.refresh_from_db()
    assert relayed.list_mode == GroupListMode.INCLUDE
    (batch,) = [callback for callback in callbacks if isinstance(callback, SyncBatch)]
    assert batch.servers == {relay_server.pk}
