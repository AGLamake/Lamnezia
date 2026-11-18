from collections.abc import Iterator

import pytest
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.registration import configure
from apps.audit.models import AuditEvent
from apps.keys.models import SubscriptionDevice
from apps.keys.services.subscription import MySubscription
from apps.keys.services.subscription_devices import (
    DeviceSignature,
    Refusal,
    SubscriptionDevices,
)
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _empty_cache() -> Iterator[None]:
    """Fetch counters and the panel settings live in the cache; no test may see another's."""
    cache.clear()
    yield
    cache.clear()


def limited_user(max_devices: int | None, personal: int | None = None) -> User:
    user = UserFactory()
    UserLimitsFactory(
        user=user,
        plan=PlanFactory(max_devices=max_devices),
        plan_assigned_at=timezone.now(),
        max_devices=personal,
    )
    return user


def happ(hwid: str, model: str = "Pixel 8") -> dict[str, str]:
    return {
        "x-hwid": hwid,
        "x-device-os": "Android",
        "x-ver-os": "15",
        "x-device-model": model,
        "User-Agent": "Happ/3.5",
    }


def fetch(client: Client, token: str, headers: dict[str, str] | None = None) -> int:
    return int(client.get(f"/sub/{token}", headers=headers or {}).status_code)


class TestSignature:
    def test_the_details_come_from_the_headers(self) -> None:
        device = DeviceSignature.of(happ("abc"))

        assert device is not None
        assert (device.model, device.system, device.app) == ("Pixel 8", "Android 15", "Happ/3.5")

    def test_none_without_a_device_id(self) -> None:
        assert DeviceSignature.of({"User-Agent": "v2rayNG/1.9", "x-hwid": " "}) is None


class TestFetch:
    def test_the_device_is_remembered(self, client: Client) -> None:
        user = limited_user(None)

        assert fetch(client, MySubscription(user).renew(), happ("abc")) == 200

        [device] = SubscriptionDevices(user).all()
        assert (device.model, device.system, device.app) == ("Pixel 8", "Android 15", "Happ/3.5")

    def test_only_a_digest_of_the_id_is_stored(self, client: Client) -> None:
        user = limited_user(None)

        fetch(client, MySubscription(user).renew(), happ("secret-hwid"))

        with connection.cursor() as cursor:
            cursor.execute("SELECT digest FROM keys_subscriptiondevice")
            [(digest,)] = cursor.fetchall()
        assert "secret-hwid" not in digest

    def test_a_known_device_passes_at_the_limit(self, client: Client) -> None:
        user = limited_user(1)
        token = MySubscription(user).renew()

        codes = [fetch(client, token, happ("abc", model)) for model in ("Pixel 8", "Pixel 9")]

        assert codes == [200, 200]
        assert [device.model for device in SubscriptionDevices(user).all()] == ["Pixel 9"]

    def test_a_new_device_over_the_limit_is_refused(self, client: Client) -> None:
        user = limited_user(1)
        token = MySubscription(user).renew()
        fetch(client, token, happ("abc"))

        response = client.get(f"/sub/{token}", headers=happ("def"))

        assert response.status_code == 403
        assert response.content.decode() == str(Refusal.TOO_MANY_DEVICES.text)
        assert len(SubscriptionDevices(user).all()) == 1

    def test_a_personal_limit_overrides_the_plan(self, client: Client) -> None:
        user = limited_user(1, personal=2)
        token = MySubscription(user).renew()

        assert [fetch(client, token, happ(hwid)) for hwid in ("a", "b", "c")] == [200, 200, 403]

    def test_without_a_device_id_by_default(self, client: Client) -> None:
        user = limited_user(1)

        assert fetch(client, MySubscription(user).renew()) == 200
        assert SubscriptionDevices(user).all() == []

    def test_without_a_device_id_when_the_panel_refuses(self, client: Client) -> None:
        configure(subscription_without_device_id=False)
        user = limited_user(None)

        response = client.get(f"/sub/{MySubscription(user).renew()}")

        assert response.status_code == 403
        assert "device ID" in response.content.decode()


class TestForget:
    def test_a_forgotten_device_frees_its_place(self, client: Client) -> None:
        user = limited_user(1)
        token = MySubscription(user).renew()
        fetch(client, token, happ("abc"))
        [device] = SubscriptionDevices(user).all()

        assert SubscriptionDevices(user).forget(str(device.pk))
        assert fetch(client, token, happ("def")) == 200

    def test_someone_elses_device_stays(self, client: Client) -> None:
        owner = limited_user(None)
        fetch(client, MySubscription(owner).renew(), happ("abc"))
        [device] = SubscriptionDevices(owner).all()

        assert not SubscriptionDevices(limited_user(None)).forget(str(device.pk))
        assert not SubscriptionDevices(owner).forget("not a number")
        assert SubscriptionDevice.objects.count() == 1

    def test_the_user_forgets_on_the_portal(self, client: Client) -> None:
        user = limited_user(2)
        fetch(client, MySubscription(user).renew(), happ("abc"))
        [device] = SubscriptionDevices(user).all()
        client.force_login(user)

        page = client.get(reverse("portal:subscription")).content.decode()
        client.post(reverse("portal:subscription"), {"action": "forget", "device": device.pk})

        assert "Pixel 8" in page
        assert SubscriptionDevices(user).all() == []

    def test_the_admin_forgets_on_the_card(self, client: Client) -> None:
        user = limited_user(None)
        fetch(client, MySubscription(user).renew(), happ("abc"))
        [device] = SubscriptionDevices(user).all()
        client.force_login(UserFactory(role=Role.ADMIN, username="admin"))

        page = client.get(reverse("dashboard:user", args=[user.pk])).content.decode()
        client.post(
            reverse("dashboard:user-subscription", args=[user.pk]),
            {"action": "forget", "device": device.pk},
        )

        assert "Pixel 8" in page
        assert SubscriptionDevices(user).all() == []
        assert AuditEvent.objects.filter(action="keys.subscription_device_forget").exists()
