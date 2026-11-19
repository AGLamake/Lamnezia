from collections.abc import Iterator
from contextlib import nullcontext

import pytest
from django.test import Client

from amnezia_core.vpn_link import decode_vpn_link
from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.actions import KeyActions
from apps.keys.services.address import CHANGE_ADDRESS, KeyAddress
from apps.keys.services.instances import Instance, instances
from apps.keys.services.profile import KeyExport, profile_host
from apps.keys.tests.conftest import HOST, SOURCE, OnCommit, Run
from apps.keys.tests.fake_server import FakeAwgServer
from apps.notifications.events import Event
from apps.notifications.models import Notification
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db

SECOND = "vpn2.example.com"
CHOOSING = "awg:2"


@pytest.fixture
def addresses(awg_server: FakeAwgServer) -> Iterator[tuple[str, ...]]:
    """Instance `awg:2` on the same server, which has another checked address."""

    def provider() -> list[Instance]:
        def host() -> nullcontext[RemoteHost]:
            return nullcontext(awg_server.host())

        return [Instance(CHOOSING, "Amsterdam", HOST, host, addresses=(SECOND,))]

    instances.register(provider)
    yield (SECOND,)
    instances.unregister(provider)


def issued(
    actions: KeyActions, run: Run, owner: User | None = None, source: str = CHOOSING
) -> AccessKey:
    run(lambda: actions.issue(source, "iPhone", owner))
    return AccessKey.objects.latest("pk")


def test_the_profile_qr_and_vpn_link_carry_the_chosen_address(
    addresses: tuple[str, ...], actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)

    assert KeyAddress(key, UserFactory()).change(SECOND)

    key.refresh_from_db()
    export = KeyExport.of(key)
    assert key.address == SECOND
    assert f"Endpoint = {SECOND}:" in export.files[0].content
    assert decode_vpn_link(export.vpn_link)["hostName"] == SECOND
    assert export.qr_codes()


def test_the_owner_is_told_and_the_change_is_journaled(
    addresses: tuple[str, ...],
    actions: KeyActions,
    run: Run,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    owner = UserFactory()
    key = issued(actions, run, owner)

    with django_capture_on_commit_callbacks(execute=True):
        KeyAddress(key, UserFactory()).change(SECOND)

    assert Notification.objects.get(event=Event.KEYS_CHANGED).user == owner
    assert AuditEvent.objects.get(action=CHANGE_ADDRESS).data == {"host": SECOND}


def test_back_to_the_own_address_clears_the_choice(
    addresses: tuple[str, ...], actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)
    choice = KeyAddress(key, UserFactory())
    choice.change(SECOND)

    assert choice.change(HOST)
    assert not choice.change(HOST)

    key.refresh_from_db()
    assert (key.address, profile_host(key.profile)) == ("", HOST)


def test_only_offered_addresses_can_be_chosen(
    addresses: tuple[str, ...], actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)

    with pytest.raises(ValueError, match=r"203.0.113.99"):
        KeyAddress(key, UserFactory()).change("203.0.113.99")


def test_a_key_without_other_addresses_has_nothing_to_choose(
    awg_server: FakeAwgServer, actions: KeyActions, run: Run
) -> None:
    """Keys of a server group get no other addresses from the instance (providers)."""
    key = issued(actions, run, source=SOURCE)

    assert KeyAddress(key, UserFactory()).choices == []


def test_a_reissued_key_keeps_the_chosen_address(
    addresses: tuple[str, ...],
    actions: KeyActions,
    run: Run,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    old = issued(actions, run)
    KeyAddress(old, UserFactory()).change(SECOND)

    with django_capture_on_commit_callbacks(execute=True):
        new = actions.reissue(old)

    new.refresh_from_db()
    assert new.status == KeyStatus.ACTIVE
    assert (new.address, profile_host(new.profile)) == (SECOND, SECOND)


def test_an_address_no_longer_offered_falls_back_to_the_own_one(
    addresses: tuple[str, ...],
) -> None:
    instance = instances.get(CHOOSING)

    assert instance.client_host(SECOND) == SECOND
    assert instance.client_host("gone.example.com") == HOST
    assert instance.client_host("") == HOST


def test_the_key_page_changes_the_address(
    addresses: tuple[str, ...], actions: KeyActions, run: Run, client: Client
) -> None:
    key = issued(actions, run)
    client.force_login(UserFactory(role=Role.ADMIN))

    page = client.get(f"/keys/{key.pk}/").content.decode()
    response = client.post(f"/keys/{key.pk}/address/", {"host": SECOND})

    assert SECOND in page
    assert response["Location"] == f"/keys/{key.pk}/"
    key.refresh_from_db()
    assert key.address == SECOND
