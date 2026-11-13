from datetime import UTC, datetime, timedelta

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.plans.models import Protocol, UserLimits
from apps.plans.services.limits import NO_PLAN, assign_plan, effective_limits
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db

GB = 1024**3
ASSIGNED = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def test_user_without_plan_may_do_nothing() -> None:
    user = UserFactory()

    limits = effective_limits(user)

    assert limits == NO_PLAN
    assert not limits.has_plan
    assert not limits.allows_another_key(0)
    assert not limits.allows_server(1)
    assert not limits.allows_protocol(Protocol.AWG2)


def test_plan_values_are_taken_as_they_are() -> None:
    plan = PlanFactory(
        traffic_limit=100 * GB,
        period_days=30,
        total_traffic_limit=500 * GB,
        max_keys=3,
        access_days=90,
        is_trial=True,
    )
    limits = UserLimitsFactory(plan=plan, plan_assigned_at=ASSIGNED)

    effective = effective_limits(limits.user)

    assert effective.plan_id == plan.pk
    assert (effective.traffic_limit, effective.period_days) == (100 * GB, 30)
    assert effective.total_traffic_limit == 500 * GB
    assert effective.max_keys == 3
    assert effective.access_until == ASSIGNED + timedelta(days=90)
    assert effective.period_start == ASSIGNED
    assert effective.is_trial


def test_empty_plan_limits_mean_no_limit() -> None:
    limits = UserLimitsFactory(plan=PlanFactory(), plan_assigned_at=ASSIGNED)

    effective = effective_limits(limits.user)

    assert effective.traffic_limit is None
    assert effective.total_traffic_limit is None
    assert effective.access_until is None
    assert effective.allows_another_key(1000)
    assert not effective.is_expired(ASSIGNED + timedelta(days=10_000))


def test_personal_overrides_win_over_the_plan() -> None:
    plan = PlanFactory(traffic_limit=100 * GB, period_days=30, max_keys=3, access_days=30)
    limits = UserLimitsFactory(
        plan=plan,
        plan_assigned_at=ASSIGNED,
        traffic_limit=250 * GB,
        period_days=7,
        total_traffic_limit=GB,
        max_keys=1,
        access_until=ASSIGNED + timedelta(days=365),
    )

    effective = effective_limits(limits.user)

    assert (effective.traffic_limit, effective.period_days) == (250 * GB, 7)
    assert effective.total_traffic_limit == GB
    assert effective.max_keys == 1
    assert effective.access_until == ASSIGNED + timedelta(days=365)


def test_zero_override_is_a_real_limit_not_inheritance() -> None:
    limits = UserLimitsFactory(plan=PlanFactory(max_keys=5, traffic_limit=GB), max_keys=0)

    effective = effective_limits(limits.user)

    assert effective.max_keys == 0
    assert not effective.allows_another_key(0)
    assert effective.traffic_limit == GB


def test_override_of_an_unlimited_plan_sets_a_limit() -> None:
    limits = UserLimitsFactory(plan=PlanFactory(), traffic_limit=10 * GB)

    assert effective_limits(limits.user).traffic_limit == 10 * GB


def test_access_end_and_expiry() -> None:
    limits = UserLimitsFactory(plan=PlanFactory(access_days=7), plan_assigned_at=ASSIGNED)
    effective = effective_limits(limits.user)
    end = ASSIGNED + timedelta(days=7)

    assert not effective.is_expired(end - timedelta(seconds=1))
    assert effective.is_expired(end)


def test_access_days_need_an_assignment_date() -> None:
    limits = UserLimitsFactory(plan=PlanFactory(access_days=7), plan_assigned_at=None)

    assert effective_limits(limits.user).access_until is None


def test_servers_and_protocols_of_the_plan() -> None:
    allowed, other = ServerFactory(), ServerFactory(host="203.0.113.99")
    plan = PlanFactory(protocols=[Protocol.AWG2])
    plan.servers.add(allowed)
    limits = UserLimitsFactory(plan=plan)

    effective = effective_limits(limits.user)

    assert effective.allows_server(allowed.pk)
    assert not effective.allows_server(other.pk)
    assert effective.allows_protocol(Protocol.AWG2)
    assert not effective.allows_protocol(Protocol.XRAY)


def test_no_servers_or_protocols_selected_allows_all() -> None:
    limits = UserLimitsFactory(plan=PlanFactory())

    effective = effective_limits(limits.user)

    assert effective.server_ids is None and effective.protocols is None
    assert effective.allows_server(12345)
    assert effective.allows_protocol(Protocol.WIREGUARD)


def test_limits_without_a_plan_are_no_plan() -> None:
    limits = UserLimitsFactory(plan=None, max_keys=10)

    assert effective_limits(limits.user) == NO_PLAN


def test_assign_plan_restarts_the_term_and_keeps_overrides() -> None:
    limits = UserLimitsFactory(plan=PlanFactory(), plan_assigned_at=ASSIGNED, max_keys=2)
    new_plan = PlanFactory(access_days=30)
    now = ASSIGNED + timedelta(days=40)

    assign_plan(limits.user, new_plan, now)

    limits.refresh_from_db()
    assert limits.plan == new_plan
    assert limits.plan_assigned_at == now
    assert limits.max_keys == 2
    assert effective_limits(limits.user).access_until == now + timedelta(days=30)


def test_assign_plan_creates_limits_and_can_take_the_plan_away() -> None:
    user = UserFactory()

    assign_plan(user, PlanFactory())
    assert UserLimits.objects.get(user=user).plan_assigned_at is not None

    assign_plan(user, None)
    assert UserLimits.objects.get(user=user).plan_assigned_at is None
    assert effective_limits(user) == NO_PLAN


@pytest.mark.parametrize(
    ("now", "start"),
    [
        (ASSIGNED, ASSIGNED),
        (ASSIGNED + timedelta(days=29, hours=23), ASSIGNED),
        (ASSIGNED + timedelta(days=30), ASSIGNED + timedelta(days=30)),
        (ASSIGNED + timedelta(days=75), ASSIGNED + timedelta(days=60)),
    ],
)
def test_periods_follow_each_other_from_the_assignment(now: datetime, start: datetime) -> None:
    limits = UserLimitsFactory(plan=PlanFactory(period_days=30), plan_assigned_at=ASSIGNED)

    assert effective_limits(limits.user).current_period_start(now) == start


def test_no_period_without_a_plan() -> None:
    assert NO_PLAN.current_period_start(ASSIGNED) is None
