from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.keys.models import AccessKey, KeyStatus
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory
from apps.traffic.models import TrafficHourly

pytestmark = pytest.mark.django_db

URL = "/portal/usage/"


def subscriber(**plan: object) -> User:
    user = UserFactory()
    assigned = timezone.now() - timedelta(days=2)
    UserLimitsFactory(user=user, plan=PlanFactory(**plan), plan_assigned_at=assigned)
    return user


def key_with_traffic(user: User, name: str, rx: int, tx: int, **fields: object) -> AccessKey:
    key = AccessKey.objects.create(
        user=user,
        source="instance:1",
        name=name,
        public_key=f"key-{name}",
        **{"status": KeyStatus.ACTIVE, **fields},
    )
    hour = timezone.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    TrafficHourly.objects.create(
        source=key.source, public_key=key.public_key, hour=hour, rx_bytes=rx, tx_bytes=tx
    )
    return key


@pytest.fixture
def user() -> User:
    return subscriber()


@pytest.fixture
def user_client(client: Client, user: User) -> Client:
    client.force_login(user)
    return client


def test_the_user_sees_traffic_of_their_keys_only(user_client: Client, user: User) -> None:
    key_with_traffic(user, "iPhone", rx=10, tx=20)
    key_with_traffic(user, "Laptop", rx=1, tx=2)
    key_with_traffic(UserFactory(), "Stranger", rx=500, tx=500)

    response = user_client.get(URL, {"period": "day"})

    traffic = response.context["traffic"]
    assert (traffic.uploaded, traffic.downloaded) == (11, 22)
    assert [row.key.name for row in traffic.keys] == ["iPhone", "Laptop"]
    assert len(response.context["chart"]["labels"]) == 24
    page = response.content.decode()
    assert '<figure class="bar-chart">' in page and "Stranger" not in page


def test_a_key_link_narrows_the_page_to_the_key(user_client: Client, user: User) -> None:
    key_with_traffic(user, "iPhone", rx=10, tx=20)
    laptop = key_with_traffic(user, "Laptop", rx=1, tx=2)

    traffic = user_client.get(URL, {"key": laptop.pk}).context["traffic"]

    assert traffic.key == laptop
    assert (traffic.uploaded, traffic.downloaded) == (1, 2)


def test_a_key_of_someone_else_is_ignored(user_client: Client, user: User) -> None:
    key_with_traffic(user, "iPhone", rx=10, tx=20)
    stranger = key_with_traffic(UserFactory(), "Stranger", rx=500, tx=500)

    response = user_client.get(URL, {"key": str(stranger.pk), "period": "year"})

    traffic = response.context["traffic"]
    assert traffic.key is None and traffic.period == "week"
    assert (traffic.uploaded, traffic.downloaded) == (10, 20)


def test_a_deleted_key_shows_only_while_it_has_traffic(user_client: Client, user: User) -> None:
    key_with_traffic(user, "Old", rx=5, tx=5, status=KeyStatus.REVOKED)
    AccessKey.objects.create(
        user=user, source="instance:1", name="Gone", public_key="gone", status=KeyStatus.REVOKED
    )

    keys = user_client.get(URL).context["traffic"].keys

    assert [row.key.name for row in keys] == ["Old"]


def test_the_forecast_warns_when_the_limit_runs_out_early(client: Client) -> None:
    user = subscriber(traffic_limit=1000, period_days=30)
    key_with_traffic(user, "iPhone", rx=200, tx=300)
    client.force_login(user)

    response = client.get(URL)

    forecast = response.context["forecast"]
    assert forecast is not None and not forecast.reached
    assert forecast.at < timezone.now() + timedelta(days=5)
    assert "the limit runs out on" in response.content.decode()


def test_the_forecast_reassures_when_the_traffic_lasts(client: Client) -> None:
    user = subscriber(traffic_limit=10**12, period_days=30)
    key_with_traffic(user, "iPhone", rx=200, tx=300)
    client.force_login(user)

    response = client.get(URL)

    assert response.context["forecast"] is None
    assert "the traffic lasts until the period resets" in response.content.decode()


def test_a_user_without_a_plan_goes_to_the_portal(client: Client) -> None:
    client.force_login(UserFactory())

    assert client.get(URL)["Location"] == "/portal/"


def test_the_home_page_links_to_the_usage(user_client: Client) -> None:
    page = user_client.get("/portal/").content.decode()

    assert f'href="{URL}"' in page
