from datetime import UTC, datetime
from typing import Any

import pytest
from django.template.defaultfilters import filesizeformat
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.plans.models import Plan, Protocol, UserLimits
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db

GB = 1024**3


@pytest.fixture
def admin_client(client: Client) -> Client:
    client.force_login(UserFactory(role=Role.ADMIN))
    return client


def plan_data(**overrides: Any) -> dict[str, Any]:
    return {"name": "Friends", "period_days": 30, **overrides}


@pytest.mark.parametrize("role", [Role.MODERATOR, Role.USER])
def test_plans_are_for_admins_only(client: Client, role: Role) -> None:
    client.force_login(UserFactory(role=role))

    assert client.get(reverse("plans:list")).status_code == 403


def test_list_shows_plans_with_their_users(admin_client: Client) -> None:
    plan = PlanFactory(name="Friends", traffic_limit=100 * GB, max_keys=3)
    UserLimitsFactory(plan=plan)

    response = admin_client.get(reverse("plans:list"))

    assert response.status_code == 200
    assert list(response.context["plans"]) == [plan]
    assert response.context["plans"][0].users == 1
    assert filesizeformat(100 * GB) in response.content.decode()


def test_create_plan_in_gigabytes(admin_client: Client) -> None:
    server = ServerFactory()

    response = admin_client.post(
        reverse("plans:add"),
        plan_data(
            traffic_limit="1.5",
            servers=[server.pk],
            protocols=[Protocol.AWG2],
            price="4.99",
            currency="eur",
        ),
    )

    assert response.status_code == 302
    plan = Plan.objects.get()
    assert plan.traffic_limit == int(1.5 * GB)
    assert plan.total_traffic_limit is None
    assert list(plan.servers.all()) == [server]
    assert plan.protocols == [Protocol.AWG2]
    assert plan.currency == "EUR"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("period_days", "0"),
        ("traffic_limit", "-1"),
        ("max_keys", "-1"),
        ("currency", "euro"),
        ("protocols", "amnezia-dns"),
    ],
)
def test_invalid_plan_is_not_saved(admin_client: Client, field: str, value: str) -> None:
    response = admin_client.post(reverse("plans:add"), plan_data(**{field: value}))

    assert response.status_code == 200
    assert field in response.context["form"].errors
    assert not Plan.objects.exists()


def test_edit_shows_traffic_in_gigabytes(admin_client: Client) -> None:
    plan = PlanFactory(traffic_limit=100 * GB)

    response = admin_client.get(reverse("plans:edit", args=[plan.pk]))

    assert 'name="traffic_limit" value="100"' in response.content.decode()


def test_delete_unused_plan(admin_client: Client) -> None:
    plan = PlanFactory()

    response = admin_client.post(reverse("plans:delete", args=[plan.pk]))

    assert response.status_code == 302
    assert not Plan.objects.exists()


def test_plan_with_users_is_not_deleted(admin_client: Client) -> None:
    limits = UserLimitsFactory()

    response = admin_client.post(reverse("plans:delete", args=[limits.plan.pk]), follow=True)

    assert Plan.objects.filter(pk=limits.plan.pk).exists()
    assert "Give them another one first" in response.content.decode()


def test_lookup_opens_the_user_page(admin_client: Client) -> None:
    user = UserFactory(username="Anna")

    response = admin_client.get(reverse("plans:lookup"), {"username": "anna"})

    assert response["Location"] == reverse("plans:user", args=[user.pk])


def test_lookup_of_unknown_user(admin_client: Client) -> None:
    response = admin_client.get(reverse("plans:lookup"), {"username": "nobody"}, follow=True)

    assert response.redirect_chain[-1][0] == reverse("plans:list")
    assert "No user with this name" in response.content.decode()


def test_user_page_shows_effective_limits(admin_client: Client) -> None:
    limits = UserLimitsFactory(plan=PlanFactory(max_keys=3), max_keys=1)

    response = admin_client.get(reverse("plans:user", args=[limits.user.pk]))

    assert response.status_code == 200
    assert response.context["effective"].max_keys == 1


def test_user_without_limits_gets_an_empty_record(admin_client: Client) -> None:
    user = UserFactory()

    response = admin_client.get(reverse("plans:user", args=[user.pk]))

    assert response.status_code == 200
    assert not response.context["effective"].has_plan
    assert UserLimits.objects.filter(user=user, plan=None).exists()


def test_assigning_a_plan_starts_its_term(admin_client: Client) -> None:
    user, plan = UserFactory(), PlanFactory(access_days=30)

    admin_client.post(
        reverse("plans:user", args=[user.pk]), {"plan": plan.pk, "traffic_limit": "10"}
    )

    limits = UserLimits.objects.get(user=user)
    assert limits.plan == plan
    assert limits.plan_assigned_at is not None
    assert limits.traffic_limit == 10 * GB


def test_saving_overrides_keeps_the_term(admin_client: Client) -> None:
    assigned = datetime(2026, 1, 1, tzinfo=UTC)
    limits = UserLimitsFactory(plan_assigned_at=assigned)

    admin_client.post(
        reverse("plans:user", args=[limits.user.pk]), {"plan": limits.plan.pk, "max_keys": "2"}
    )

    limits.refresh_from_db()
    assert limits.max_keys == 2
    assert limits.plan_assigned_at == assigned


def test_plan_changes_are_in_the_audit_log(admin_client: Client) -> None:
    admin_client.post(reverse("plans:add"), plan_data())
    plan = Plan.objects.get()
    admin_client.post(reverse("plans:edit", args=[plan.pk]), plan_data(max_keys="2"))
    admin_client.post(reverse("plans:delete", args=[plan.pk]))

    events = list(AuditEvent.objects.order_by("pk").values_list("action", "target_repr", "data"))
    assert events == [
        ("plans.create", "Friends", {}),
        ("plans.update", "Friends", {"changed": ["max_keys"]}),
        ("plans.delete", "Friends", {}),
    ]


def test_refused_delete_leaves_no_audit_event(admin_client: Client) -> None:
    limits = UserLimitsFactory()

    admin_client.post(reverse("plans:delete", args=[limits.plan.pk]))

    assert not AuditEvent.objects.exists()


def test_limit_changes_are_in_the_audit_log(admin_client: Client) -> None:
    user, plan = UserFactory(username="anna"), PlanFactory()

    admin_client.post(reverse("plans:user", args=[user.pk]), {"plan": plan.pk, "max_keys": "2"})
    admin_client.post(reverse("plans:user", args=[user.pk]), {"plan": plan.pk, "max_keys": "2"})

    event = AuditEvent.objects.get()
    assert (event.action, event.target_repr) == ("plans.limits", "anna")
    assert event.data == {"plan_id": plan.pk, "changed": ["plan", "max_keys"]}


@pytest.fixture
def speed_runs(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    runs: list[None] = []
    monkeypatch.setattr(
        "apps.plans.services.changes.apply_speed_limits.delay", lambda: runs.append(None)
    )
    return runs


def test_new_speed_of_a_plan_is_applied_at_once(
    admin_client: Client, speed_runs: list[None], django_capture_on_commit_callbacks: Any
) -> None:
    plan = PlanFactory(name="Friends")

    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(reverse("plans:edit", args=[plan.pk]), plan_data(speed_limit="10"))
        admin_client.post(reverse("plans:edit", args=[plan.pk]), plan_data(speed_limit="10"))

    assert len(speed_runs) == 1
    plan.refresh_from_db()
    assert plan.speed_limit == 10


def test_personal_speed_or_a_new_plan_is_applied_at_once(
    admin_client: Client, speed_runs: list[None], django_capture_on_commit_callbacks: Any
) -> None:
    user = UserFactory()
    plan = PlanFactory()
    url = reverse("plans:user", args=[user.pk])

    with django_capture_on_commit_callbacks(execute=True):
        admin_client.post(url, {"plan": plan.pk})
        admin_client.post(url, {"plan": plan.pk, "max_keys": "2"})
        admin_client.post(url, {"plan": plan.pk, "max_keys": "2", "speed_limit": "5"})

    assert len(speed_runs) == 2
    assert UserLimits.objects.get(user=user).speed_limit == 5
