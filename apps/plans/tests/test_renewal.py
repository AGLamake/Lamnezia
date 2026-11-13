from datetime import UTC, datetime, timedelta

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.plans.services.limits import effective_limits
from apps.plans.services.renewal import extend_access, renew
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


def test_extend_moves_a_running_term_from_its_end() -> None:
    limits = UserLimitsFactory(
        plan=PlanFactory(access_days=30), plan_assigned_at=NOW - timedelta(days=10)
    )

    end = extend_access(limits, 30, NOW)

    assert end == NOW + timedelta(days=50)
    assert effective_limits(limits.user).access_until == end


def test_extend_counts_an_ended_term_from_now() -> None:
    limits = UserLimitsFactory(access_until=NOW - timedelta(days=5))

    assert extend_access(limits, 7, NOW) == NOW + timedelta(days=7)


def test_extend_leaves_access_without_end_date() -> None:
    limits = UserLimitsFactory(plan=PlanFactory(access_days=None), plan_assigned_at=NOW)

    assert extend_access(limits, 7, NOW) is None
    limits.refresh_from_db()
    assert limits.access_until is None


def test_renewing_the_same_plan_extends_it_and_keeps_its_periods() -> None:
    assigned = NOW - timedelta(days=20)
    limits = UserLimitsFactory(plan=PlanFactory(access_days=30), plan_assigned_at=assigned)

    renew(limits.user, limits.plan, 30, NOW)

    limits.refresh_from_db()
    assert limits.plan_assigned_at == assigned
    assert limits.access_until == assigned + timedelta(days=60)


def test_renewing_an_expired_plan_counts_from_now() -> None:
    limits = UserLimitsFactory(
        plan=PlanFactory(access_days=30), plan_assigned_at=NOW - timedelta(days=45)
    )

    renew(limits.user, limits.plan, 30, NOW)

    assert effective_limits(limits.user).access_until == NOW + timedelta(days=30)


def test_another_plan_starts_its_term_now_and_keeps_overrides() -> None:
    limits = UserLimitsFactory(
        plan_assigned_at=NOW - timedelta(days=3),
        max_keys=7,
        access_until=NOW + timedelta(days=300),
    )
    plan = PlanFactory(access_days=30)

    renew(limits.user, plan, 30, NOW)

    limits.refresh_from_db()
    assert (limits.plan, limits.plan_assigned_at, limits.max_keys) == (plan, NOW, 7)
    assert effective_limits(limits.user).access_until == NOW + timedelta(days=30)


def test_user_without_limits_gets_the_plan() -> None:
    user = UserFactory()
    plan = PlanFactory(access_days=None)

    renew(user, plan, None, NOW)

    effective = effective_limits(user)
    assert effective.plan_id == plan.pk
    assert effective.access_until is None
