import base64
from collections.abc import Iterator
from contextlib import nullcontext
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.db import connection
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from amnezia_core.xray import XRAY, client_fingerprint
from apps.accounts.models import Role, Status, User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.core.models import KeyRevealPolicy, SiteSettings
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.instances import Instance, instances
from apps.keys.services.profile import XrayKeyProfile
from apps.keys.services.subscription import MySubscription, SubscriptionFeed
from apps.keys.services.xray_profiles import BUILD_PROFILES
from apps.keys.tests.conftest import HOST
from apps.keys.tests.test_views import issued_key
from apps.operations.models import Operation
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory
from apps.traffic.models import TrafficHourly

pytestmark = pytest.mark.django_db

XRAY_SOURCE = "xray:1"


@pytest.fixture(autouse=True)
def _empty_cache() -> Iterator[None]:
    """Fetch counters live in the cache; no test may see another's."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def user() -> User:
    user = UserFactory()
    UserLimitsFactory(user=user, plan=PlanFactory(), plan_assigned_at=timezone.now())
    return user


def xray_key(user: User, name: str, host: str = HOST, **fields: object) -> AccessKey:
    client_id = f"{name}-uuid"
    profile = XrayKeyProfile(
        host, 443, "xtls-rprx-vision", "www.example.com", "www.example.com", "chrome", "pbk", "ab"
    )
    fields.setdefault("profile", profile.to_json())
    return issued_key(
        user=user,
        source=XRAY_SOURCE,
        name=name,
        public_key=client_fingerprint(client_id),
        private_key=client_id,
        client_ip=None,
        **fields,
    )


def fetched(client: Client, token: str) -> list[str]:
    response = client.get(f"/sub/{token}")
    assert response.status_code == 200
    return base64.b64decode(response.content).decode().splitlines()


def reveal_policy(policy: KeyRevealPolicy) -> None:
    site = SiteSettings.load()
    site.key_reveal_policy = policy
    site.save()


class TestFeed:
    def test_the_link_gives_the_active_xray_keys(self, client: Client, user: User) -> None:
        xray_key(user, "phone")
        xray_key(user, "old", status=KeyStatus.DISABLED_MANUAL)
        issued_key(user=user, name="awg")
        token = MySubscription(user).renew()

        links = fetched(client, token)

        assert links == [SubscriptionFeed.find(token).links[0]]  # type: ignore[union-attr]
        assert links[0].startswith(f"vless://phone-uuid@{HOST}:443?")
        assert links[0].endswith("#phone")

    def test_clients_learn_the_name_and_how_often_to_refresh(
        self, client: Client, user: User
    ) -> None:
        response = client.get(f"/sub/{MySubscription(user).renew()}")

        assert response["profile-update-interval"] == "12"
        assert base64.b64decode(response["profile-title"].removeprefix("base64:")) == b"Lamnezia"
        assert response["Cache-Control"] == "no-store"

    def test_an_ipv6_host_is_in_brackets(self, client: Client, user: User) -> None:
        xray_key(user, "phone", host="2001:db8::1")

        [link] = fetched(client, MySubscription(user).renew())

        assert "@[2001:db8::1]:443?" in link

    def test_a_new_link_replaces_the_old_one(self, client: Client, user: User) -> None:
        subscription = MySubscription(user)
        old = subscription.renew()

        new = subscription.renew()

        assert client.get(f"/sub/{old}").status_code == 404
        assert client.get(f"/sub/{new}").status_code == 200
        assert subscription.token() == new

    def test_the_token_is_kept_encrypted(self, user: User) -> None:
        token = MySubscription(user).renew()

        with connection.cursor() as cursor:
            cursor.execute("SELECT token, digest FROM keys_subscription")
            stored = cursor.fetchone()

        assert token not in "".join(stored)

    @pytest.mark.parametrize("change", ["blocked", "deactivated", "shown_once"])
    def test_the_link_stops_working(self, client: Client, user: User, change: str) -> None:
        token = MySubscription(user).renew()
        if change == "blocked":
            user.status = Status.BLOCKED
        if change == "deactivated":
            user.is_active = False
        user.save()
        if change == "shown_once":
            reveal_policy(KeyRevealPolicy.ONCE)

        assert client.get(f"/sub/{token}").status_code == 404

    def test_an_unknown_token_is_missing(self, client: Client) -> None:
        assert client.get("/sub/nothing").status_code == 404

    def test_an_imported_key_asks_for_its_profile(self, client: Client, user: User) -> None:
        def provider() -> list[Instance]:
            return [Instance(XRAY_SOURCE, "Frankfurt", HOST, nullcontext, XRAY)]  # type: ignore[arg-type]

        xray_key(user, "imported", profile="")
        instances.register(provider)
        try:
            assert fetched(client, MySubscription(user).renew()) == []
        finally:
            instances.unregister(provider)

        assert Operation.objects.get(kind=BUILD_PROFILES).params == {"source": XRAY_SOURCE}


class TestPortal:
    @pytest.fixture
    def user_client(self, client: Client, user: User) -> Client:
        client.force_login(user)
        return client

    def test_the_user_creates_and_deletes_the_link(self, user_client: Client, user: User) -> None:
        assert user_client.get("/portal/subscription/").context["url"] is None

        user_client.post("/portal/subscription/", {"action": "renew"})
        token = MySubscription(user).token()
        page = user_client.get("/portal/subscription/").content.decode()
        assert f"/sub/{token}" in page

        user_client.post("/portal/subscription/", {"action": "delete"})
        assert MySubscription(user).token() is None

    def test_there_is_no_link_when_keys_are_shown_once(self, user_client: Client) -> None:
        reveal_policy(KeyRevealPolicy.ONCE)

        assert user_client.get("/portal/subscription/").status_code == 404
        assert user_client.post("/portal/subscription/").status_code == 404

    def test_the_home_page_offers_it_to_the_owners_of_xray_keys(
        self, user_client: Client, user: User
    ) -> None:
        def provider() -> list[Instance]:
            return [Instance(XRAY_SOURCE, "XRay", HOST, nullcontext, XRAY)]  # type: ignore[arg-type]

        assert not user_client.get("/portal/").context["offers_subscription"]

        xray_key(user, "phone")
        instances.register(provider)
        try:
            assert user_client.get("/portal/").context["offers_subscription"]
        finally:
            instances.unregister(provider)


def subscriber(**plan: object) -> User:
    user = UserFactory()
    assigned = timezone.now() - timedelta(days=45)
    UserLimitsFactory(user=user, plan=PlanFactory(**plan), plan_assigned_at=assigned)
    return user


def traffic(key: AccessKey, ago: timedelta, rx: int, tx: int) -> None:
    hour = (timezone.now() - ago).replace(minute=0, second=0, microsecond=0)
    TrafficHourly.objects.create(
        source=key.source, public_key=key.public_key, hour=hour, rx_bytes=rx, tx_bytes=tx
    )


def userinfo(client: Client, user: User) -> dict[str, int]:
    header = client.get(f"/sub/{MySubscription(user).renew()}")["subscription-userinfo"]
    return {name: int(value) for name, value in (part.split("=") for part in header.split("; "))}


class TestUserinfo:
    def test_the_period_traffic_against_its_limit(self, client: Client) -> None:
        user = subscriber(traffic_limit=1000, period_days=30, access_days=None)
        key = xray_key(user, "phone")
        traffic(key, timedelta(days=40), rx=500, tx=500)  # the period before
        traffic(key, timedelta(days=2), rx=10, tx=200)

        assert userinfo(client, user) == {"upload": 10, "download": 200, "total": 1000, "expire": 0}

    def test_the_total_limit_counts_since_the_plan_was_assigned(self, client: Client) -> None:
        user = subscriber(traffic_limit=None, total_traffic_limit=5000, period_days=30)
        key = xray_key(user, "phone")
        traffic(key, timedelta(days=40), rx=100, tx=300)
        traffic(key, timedelta(days=2), rx=10, tx=20)

        info = userinfo(client, user)

        assert (info["upload"], info["download"], info["total"]) == (110, 320, 5000)

    def test_of_two_limits_the_one_with_less_left(self, client: Client) -> None:
        user = subscriber(traffic_limit=10_000, total_traffic_limit=1000, period_days=30)
        traffic(xray_key(user, "phone"), timedelta(days=2), rx=100, tx=100)

        assert userinfo(client, user)["total"] == 1000

    def test_unlimited_traffic_is_total_zero(self, client: Client) -> None:
        user = subscriber(traffic_limit=None, total_traffic_limit=None)
        traffic(xray_key(user, "phone"), timedelta(days=1), rx=7, tx=9)

        info = userinfo(client, user)

        assert (info["upload"], info["download"], info["total"]) == (7, 9, 0)

    def test_the_access_term_is_the_expiry(self, client: Client) -> None:
        user = subscriber(access_days=60)
        assigned = user.limits.plan_assigned_at
        assert assigned is not None

        until = assigned + timedelta(days=60)
        assert userinfo(client, user)["expire"] == int(until.timestamp())

    def test_no_header_without_a_plan(self, client: Client) -> None:
        user = UserFactory()

        response = client.get(f"/sub/{MySubscription(user).renew()}")

        assert "subscription-userinfo" not in response


class TestRateLimit:
    @override_settings(SUBSCRIPTION_RATE_LIMIT=2)
    def test_frequent_fetches_get_429(self, client: Client, user: User) -> None:
        token = MySubscription(user).renew()

        codes = [client.get(f"/sub/{token}").status_code for _ in range(3)]

        assert codes == [200, 200, 429]
        assert client.get(f"/sub/{token}")["Retry-After"] == "60"

    @override_settings(SUBSCRIPTION_RATE_LIMIT=1)
    def test_guessing_tokens_counts_too(self, client: Client) -> None:
        assert client.get("/sub/one").status_code == 404
        assert client.get("/sub/two").status_code == 429

    @override_settings(SUBSCRIPTION_RATE_LIMIT=1)
    def test_each_address_has_its_own_count(self, client: Client, user: User) -> None:
        token = MySubscription(user).renew()

        assert client.get(f"/sub/{token}", REMOTE_ADDR="192.0.2.1").status_code == 200
        assert client.get(f"/sub/{token}", REMOTE_ADDR="192.0.2.2").status_code == 200

    @override_settings(SUBSCRIPTION_RATE_LIMIT=0)
    def test_zero_turns_the_limit_off(self, client: Client, user: User) -> None:
        token = MySubscription(user).renew()

        assert all(client.get(f"/sub/{token}").status_code == 200 for _ in range(5))


class TestAdmin:
    @pytest.fixture
    def admin_client(self, client: Client) -> Client:
        client.force_login(UserFactory(role=Role.ADMIN, username="admin"))
        return client

    def test_the_card_shows_the_link(self, admin_client: Client, user: User) -> None:
        token = MySubscription(user).renew()

        page = admin_client.get(reverse("dashboard:user", args=[user.pk])).content.decode()

        assert f"/sub/{token}" in page

    def test_the_admin_creates_and_deletes_the_link(self, admin_client: Client, user: User) -> None:
        url = reverse("dashboard:user-subscription", args=[user.pk])

        admin_client.post(url)
        assert MySubscription(user).token() is not None

        admin_client.post(url, {"action": "delete"})
        assert MySubscription(user).token() is None
        actions = list(AuditEvent.objects.order_by("pk").values_list("action", flat=True))
        assert actions == ["keys.subscription_renew", "keys.subscription_delete"]

    def test_nothing_when_keys_are_shown_once(self, admin_client: Client, user: User) -> None:
        reveal_policy(KeyRevealPolicy.ONCE)

        page = admin_client.get(reverse("dashboard:user", args=[user.pk]))
        response = admin_client.post(reverse("dashboard:user-subscription", args=[user.pk]))

        assert not page.context["offers_subscription"]
        assert response.status_code == 404

    def test_a_user_cannot_change_the_link(self, client: Client, user: User) -> None:
        client.force_login(user)

        client.post(reverse("dashboard:user-subscription", args=[user.pk]))

        assert MySubscription(user).token() is None
