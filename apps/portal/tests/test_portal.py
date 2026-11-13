import re
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from datetime import timedelta
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from amnezia_core.xray import XRAY
from apps.accounts.models import Status, User
from apps.accounts.tests.factories import UserFactory
from apps.core.models import KeyRevealPolicy, SiteSettings
from apps.core.templatetags.ui import ICONS
from apps.guides.services.devices import Device
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.instances import Instance, instances
from apps.keys.tests.conftest import SOURCE
from apps.keys.tests.fake_server import FakeAwgServer
from apps.keys.tests.test_views import imported_key, issued_key
from apps.operations.models import Operation, OperationStatus
from apps.plans.models import Protocol
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory

pytestmark = pytest.mark.django_db

OnCommit = Callable[..., AbstractContextManager[Any]]
ANDROID_UA = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Mobile Safari/537.36"


def subscriber(**plan: Any) -> User:
    user = UserFactory()
    UserLimitsFactory(user=user, plan=PlanFactory(**plan), plan_assigned_at=timezone.now())
    return user


@pytest.fixture
def user() -> User:
    return subscriber(max_keys=2)


@pytest.fixture
def user_client(client: Client, user: User) -> Client:
    client.force_login(user)
    return client


def reveal_policy(policy: KeyRevealPolicy) -> None:
    site = SiteSettings.load()
    site.key_reveal_policy = policy
    site.save()


def test_pending_user_sees_only_the_waiting_message(client: Client) -> None:
    user = subscriber()
    user.status = Status.PENDING
    user.save()
    client.force_login(user)

    page = client.get("/portal/").content.decode()

    assert "waits for approval" in page
    assert "Get a key" not in page
    assert client.get("/portal/keys/new/").status_code == 302


def test_user_without_plan_is_told_to_ask_for_one(client: Client) -> None:
    client.force_login(UserFactory())

    assert "no plan yet" in client.get("/portal/").content.decode()


def test_get_a_key_in_three_steps(
    user_client: Client,
    user: User,
    awg_server: FakeAwgServer,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    form = user_client.get("/portal/keys/new/", headers={"user-agent": ANDROID_UA})
    assert re.search(r'value="android"[^>]* checked', form.content.decode())

    with django_capture_on_commit_callbacks(execute=True):
        response = user_client.post("/portal/keys/new/", {"source": SOURCE, "device": "android"})

    key = AccessKey.objects.get(user=user)
    assert response["Location"] == f"/portal/keys/{key.pk}/?device=android"
    assert (key.name, key.status) == ("Android", KeyStatus.ACTIVE)
    page = user_client.get(response["Location"]).content.decode()
    assert "/guides/android/" in page and f"/portal/keys/{key.pk}/show/" in page
    shown = user_client.post(f"/portal/keys/{key.pk}/show/").content.decode()
    assert "vpn://" in shown and "<svg" in shown and "data:text/plain" in shown


def test_the_only_server_is_preset_and_the_device_comes_first(
    user_client: Client, awg_server: FakeAwgServer
) -> None:
    page = user_client.get("/portal/keys/new/").content.decode()

    assert f'type="hidden" name="source" value="{SOURCE}"' in page
    assert 'type="radio" name="source"' not in page
    assert page.index('name="device"') < page.index('name="source"')


def test_every_device_choice_has_its_system_icon(
    user_client: Client, awg_server: FakeAwgServer
) -> None:
    page = user_client.get("/portal/keys/new/").content.decode()

    for device in Device.values:
        assert ICONS[f"os-{device}"] in page, device


def test_key_page_polls_while_the_key_is_issued(user_client: Client, user: User) -> None:
    key = issued_key(user=user, status=KeyStatus.ISSUING, profile="")

    state = user_client.get(f"/portal/keys/{key.pk}/state/").content.decode()

    assert "hx-trigger" in state and f"/portal/keys/{key.pk}/show/" not in state


def test_key_page_waits_for_the_profile_of_an_imported_xray_key(
    user_client: Client, user: User
) -> None:
    def provider() -> list[Instance]:
        return [Instance("xray:1", "Frankfurt", "203.0.113.20", nullcontext, XRAY)]  # type: ignore[arg-type]

    key = issued_key(user=user, source="xray:1", private_key="uuid", profile="")
    instances.register(provider)
    try:
        waiting = user_client.get(f"/portal/keys/{key.pk}/state/").content.decode()
        Operation.objects.update(status=OperationStatus.FAILED)
        failed = user_client.get(f"/portal/keys/{key.pk}/state/").content.decode()
    finally:
        instances.unregister(provider)

    assert "hx-trigger" in waiting and "being prepared" in waiting
    assert "hx-trigger" not in failed and "cannot be prepared right now" in failed


def test_key_limit_cannot_be_exceeded(
    user_client: Client, user: User, awg_server: FakeAwgServer
) -> None:
    issued_key(user=user)
    issued_key(user=user, public_key="second")
    issued_key(user=user, public_key="old", status=KeyStatus.REVOKED)

    assert "Get a key" not in user_client.get("/portal/").content.decode()
    response = user_client.post("/portal/keys/new/", {"source": SOURCE, "device": "ios"})

    assert response["Location"] == "/portal/"
    assert AccessKey.objects.filter(user=user).count() == 3


def test_server_outside_the_plan_is_not_offered(client: Client, awg_server: FakeAwgServer) -> None:
    client.force_login(subscriber(protocols=[Protocol.XRAY]))

    page = client.get("/portal/keys/new/").content.decode()
    response = client.post("/portal/keys/new/", {"source": SOURCE, "device": "ios"})

    assert "No server is available" in page
    assert response.status_code == 200
    assert not AccessKey.objects.exists()


def test_a_key_being_revoked_takes_no_place_in_the_limit(
    user_client: Client, user: User, awg_server: FakeAwgServer
) -> None:
    issued_key(user=user)
    issued_key(user=user, public_key="leaving", status=KeyStatus.REVOKING)

    page = user_client.get("/portal/").content.decode()

    assert "Get a key" in page and "leaving" not in page


def test_server_under_maintenance_is_not_offered(client: Client) -> None:
    def provider() -> list[Instance]:
        connect = nullcontext  # no key is issued, so nothing connects
        return [Instance(SOURCE, "Amsterdam", "203.0.113.1", connect, maintenance=True)]  # type: ignore[arg-type]

    instances.register(provider)
    try:
        client.force_login(subscriber())
        page = client.get("/portal/keys/new/").content.decode()
        response = client.post("/portal/keys/new/", {"source": SOURCE, "device": "ios"})
    finally:
        instances.unregister(provider)

    assert "No server is available" in page
    assert response.status_code == 200
    assert not AccessKey.objects.exists()


def test_expired_access_gives_no_new_keys(client: Client, awg_server: FakeAwgServer) -> None:
    user = UserFactory()
    UserLimitsFactory(
        user=user,
        plan=PlanFactory(access_days=1),
        plan_assigned_at=timezone.now() - timedelta(days=2),
    )
    client.force_login(user)

    page = client.get("/portal/").content.decode()

    assert "access has ended" in page
    assert client.get("/portal/keys/new/").status_code == 302


def test_always_policy_shows_the_key_every_time(user_client: Client, user: User) -> None:
    key = issued_key(user=user)

    for _attempt in range(2):
        assert "vpn://" in user_client.post(f"/portal/keys/{key.pk}/show/").content.decode()
    key.refresh_from_db()
    assert key.first_viewed_at is not None


def test_once_policy_shows_the_key_only_once_and_reissue_gives_a_new_one(
    user_client: Client, user: User, awg_server: FakeAwgServer
) -> None:
    reveal_policy(KeyRevealPolicy.ONCE)
    key = issued_key(user=user)

    first = user_client.post(f"/portal/keys/{key.pk}/show/")
    second = user_client.post(f"/portal/keys/{key.pk}/show/")

    assert "vpn://" in first.content.decode()
    assert second["Location"] == f"/portal/keys/{key.pk}/"
    page = user_client.get(second["Location"]).content.decode()
    assert "already been shown" in page and f"/portal/keys/{key.pk}/show/" not in page

    response = user_client.post(f"/portal/keys/{key.pk}/reissue/")

    new = AccessKey.objects.exclude(pk=key.pk).get(user=user)
    assert response["Location"] == f"/portal/keys/{new.pk}/"
    assert (new.name, new.source, new.first_viewed_at) == (key.name, SOURCE, None)
    assert set(Operation.objects.values_list("kind", flat=True)) == {"keys.reissue"}


def test_imported_key_cannot_be_shown_only_reissued(user_client: Client, user: User) -> None:
    key = imported_key()
    key.user = user
    key.save()

    page = user_client.get(f"/portal/keys/{key.pk}/").content.decode()

    assert "cannot be shown" in page
    assert user_client.post(f"/portal/keys/{key.pk}/show/").status_code == 302


def test_rename_and_delete_start_operations(user_client: Client, user: User) -> None:
    key = issued_key(user=user)

    user_client.post(f"/portal/keys/{key.pk}/rename/", {"name": " Laptop "})
    response = user_client.post(f"/portal/keys/{key.pk}/delete/")

    assert response["Location"] == "/portal/"
    rename, revoke = Operation.objects.order_by("pk")
    assert (rename.kind, rename.params["name"]) == ("keys.rename", "Laptop")
    assert revoke.kind == "keys.revoke"


def test_a_second_press_while_the_key_is_revoked_does_nothing(
    user_client: Client, user: User, awg_server: FakeAwgServer
) -> None:
    key = issued_key(user=user)
    user_client.post(f"/portal/keys/{key.pk}/reissue/")

    again = user_client.post(f"/portal/keys/{key.pk}/reissue/")
    deleted = user_client.post(f"/portal/keys/{key.pk}/delete/")

    assert again["Location"] == deleted["Location"] == "/portal/"
    assert AccessKey.objects.filter(user=user).count() == 2
    assert Operation.objects.filter(kind__in=("keys.revoke", "keys.reissue")).count() == 1


def test_keys_of_other_users_are_not_found(user_client: Client) -> None:
    key = issued_key(user=UserFactory())

    for path in ("", "state/"):
        assert user_client.get(f"/portal/keys/{key.pk}/{path}").status_code == 404
    for path in ("show/", "rename/", "reissue/", "delete/"):
        assert user_client.post(f"/portal/keys/{key.pk}/{path}").status_code == 404
    assert not Operation.objects.exists()


def test_guest_goes_to_login(client: Client) -> None:
    response = client.get("/portal/keys/1/")

    assert response.status_code == 302 and "/accounts/login/" in response["Location"]
