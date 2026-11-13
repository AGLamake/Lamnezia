from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any

import pytest
from django.template.defaultfilters import filesizeformat
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Role, Status, User
from apps.accounts.tests.factories import UserFactory
from apps.dashboard.models import BatchAction, Outcome, UserBatch
from apps.keys.models import AccessKey, KeyStatus
from apps.operations.models import Operation
from apps.plans.models import UserLimits
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory
from apps.protocols.models import InstanceOrigin, ProtocolInstance
from apps.servers.tests.factories import ServerFactory
from apps.traffic.models import TrafficHourly

pytestmark = pytest.mark.django_db

OnCommit = Callable[..., AbstractContextManager[Any]]
GB = 1024**3


@pytest.fixture
def admin_client(client: Client) -> Client:
    client.force_login(UserFactory(role=Role.ADMIN, username="admin"))
    return client


def key_of(
    user: User, source: str = "instance:1", status: KeyStatus = KeyStatus.ACTIVE
) -> AccessKey:
    return AccessKey.objects.create(
        user=user,
        source=source,
        name="iPhone",
        public_key=f"key-{AccessKey.objects.count()}",
        status=status,
    )


def listed(client: Client, **query: Any) -> list[str]:
    response = client.get(reverse("dashboard:users"), query)
    assert response.status_code == 200
    return [row.user.username for row in response.context["rows"]]


def test_users_are_for_the_staff_only(client: Client) -> None:
    user = UserFactory(role=Role.USER)
    client.force_login(user)

    assert client.get(reverse("dashboard:users")).status_code == 403
    assert client.get(reverse("dashboard:user", args=[user.pk])).status_code == 403
    assert client.post(reverse("dashboard:users-batch")).status_code == 403


def test_list_shows_plan_access_and_active_keys(admin_client: Client) -> None:
    limits: UserLimits = UserLimitsFactory(
        user__username="alice",
        plan__name="Friends",
        plan__access_days=30,
        plan_assigned_at=datetime(2026, 11, 1, tzinfo=UTC),
    )
    key_of(limits.user)
    key_of(limits.user, status=KeyStatus.REVOKED)

    response = admin_client.get(reverse("dashboard:users"))

    row = next(row for row in response.context["rows"] if row.user.username == "alice")
    assert (row.plan.name, row.access_until, row.user.active_keys) == (
        "Friends",
        datetime(2026, 12, 1, tzinfo=UTC),
        1,
    )
    assert "Friends" in response.content.decode()


def test_list_filters_by_plan_status_server_and_name(admin_client: Client) -> None:
    plan = PlanFactory()
    UserLimitsFactory(user__username="alice", plan=plan)
    UserFactory(username="bob", status=Status.PENDING, email="bob@mail.test")
    carol = UserFactory(username="carol")
    server = ServerFactory()
    instance = ProtocolInstance.objects.create(
        server=server,
        container="amnezia-awg2",
        port=443,
        subnet="10.8.1.0/24",
        public_key="pub",
        private_key="priv",
        origin=InstanceOrigin.IMPORTED,
    )
    key_of(carol, source=instance.source)

    assert listed(admin_client, plan=plan.pk) == ["alice"]
    assert listed(admin_client, plan="none") == ["admin", "bob", "carol"]
    assert listed(admin_client, status=Status.PENDING) == ["bob"]
    assert listed(admin_client, server=server.pk) == ["carol"]
    assert listed(admin_client, search="mail.test") == ["bob"]


def test_card_shows_overrides_traffic_and_keys(admin_client: Client) -> None:
    limits: UserLimits = UserLimitsFactory(
        plan__traffic_limit=100 * GB,
        traffic_limit=50 * GB,
        plan_assigned_at=datetime(2026, 11, 1, tzinfo=UTC),
    )
    key = key_of(limits.user)
    TrafficHourly.objects.create(
        source=key.source,
        public_key=key.public_key,
        hour=datetime(2026, 11, 2, tzinfo=UTC),
        rx_bytes=GB,
        tx_bytes=GB,
    )

    response = admin_client.get(reverse("dashboard:user", args=[limits.user.pk]))

    card = response.context["card"]
    assert card.effective.traffic_limit == 50 * GB
    assert card.usage.total == 2 * GB
    assert [k for k, _title in card.keys] == [key]
    page = response.content.decode()
    assert filesizeformat(100 * GB) in page
    assert filesizeformat(50 * GB) in page
    assert reverse("keys:detail", args=[key.pk]) in page


def test_card_of_a_user_without_a_plan(admin_client: Client) -> None:
    user = UserFactory()

    response = admin_client.get(reverse("dashboard:user", args=[user.pk]))

    assert response.status_code == 200
    assert response.context["card"].plan is None


def test_batch_runs_and_opens_the_report(
    admin_client: Client, django_capture_on_commit_callbacks: OnCommit
) -> None:
    user = UserFactory()
    key_of(user)

    with django_capture_on_commit_callbacks(execute=False):
        response = admin_client.post(
            reverse("dashboard:users-batch"), {"users": [user.pk], "action": BatchAction.DISABLE}
        )

    batch = UserBatch.objects.get()
    assert response.url == reverse("dashboard:batch", args=[batch.pk])  # type: ignore[attr-defined]
    assert batch.items.get().outcome == Outcome.DONE
    assert Operation.objects.count() == 1
    report = admin_client.get(response.url, headers={"accept-language": "en"})  # type: ignore[attr-defined]
    assert report.status_code == 200
    # Operations are still waiting for the worker, so the report keeps polling.
    assert reverse("dashboard:batch-progress", args=[batch.pk]) in report.content.decode()


def test_batch_with_a_mistake_goes_back_to_the_filtered_list(admin_client: Client) -> None:
    user = UserFactory()

    response = admin_client.post(
        reverse("dashboard:users-batch"),
        {"users": [user.pk], "action": BatchAction.EXTEND, "filters": "status=active"},
    )

    assert response.url == f"{reverse('dashboard:users')}?status=active"  # type: ignore[attr-defined]
    assert not UserBatch.objects.exists()


def test_extend_and_change_plan_from_the_list(admin_client: Client) -> None:
    limits: UserLimits = UserLimitsFactory(
        plan__access_days=30, plan_assigned_at=datetime(2026, 11, 1, tzinfo=UTC)
    )
    plan = PlanFactory()

    admin_client.post(
        reverse("dashboard:users-batch"),
        {"users": [limits.user.pk], "action": BatchAction.EXTEND, "days": 10},
    )
    admin_client.post(
        reverse("dashboard:users-batch"),
        {"users": [limits.user.pk], "action": BatchAction.CHANGE_PLAN, "plan": plan.pk},
    )

    limits.refresh_from_db()
    assert limits.access_until == datetime(2026, 12, 11, tzinfo=UTC)
    assert limits.plan == plan
