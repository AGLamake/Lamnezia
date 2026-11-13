from datetime import UTC, datetime, timedelta

import pytest
from django.db.models import Sum
from django.test import override_settings

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.keys.models import AccessKey, KeyStatus
from apps.plans.models import UserLimits
from apps.plans.services.limits import EffectiveLimits
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory
from apps.traffic.models import TrafficHourly
from apps.traffic.services.limits import LimitEnforcer
from apps.traffic.services.rollup import TrafficRollup

pytestmark = pytest.mark.django_db

SOURCE = "awg:1"
HOUR = timedelta(hours=1)
KEEP = timedelta(days=2)


def key_of(user: User | None = None) -> AccessKey:
    return AccessKey.objects.create(
        user=user,
        source=SOURCE,
        name="iPhone",
        public_key=f"key-{AccessKey.objects.count()}",
        status=KeyStatus.ACTIVE,
    )


def hours(key: AccessKey, start: datetime, count: int) -> None:
    TrafficHourly.objects.bulk_create(
        TrafficHourly(
            source=key.source,
            public_key=key.public_key,
            hour=start + HOUR * n,
            rx_bytes=100 + n,
            tx_bytes=10 * n,
        )
        for n in range(count)
    )


def totals() -> tuple[int, int]:
    sums = TrafficHourly.objects.aggregate(rx=Sum("rx_bytes"), tx=Sum("tx_bytes"))
    return sums["rx"], sums["tx"]


def test_hours_of_an_old_day_fold_into_one_row_and_recent_hours_stay() -> None:
    key = key_of()
    hours(key, datetime(2026, 11, 1, tzinfo=UTC), 24)
    hours(key, datetime(2026, 11, 5, tzinfo=UTC), 5)
    before = totals()

    assert TrafficRollup(KEEP, datetime(2026, 11, 5, 6, tzinfo=UTC)).run() == 23

    assert totals() == before
    old = TrafficHourly.objects.get(hour__lt=datetime(2026, 11, 2, tzinfo=UTC))
    assert old.hour == datetime(2026, 11, 1, tzinfo=UTC)
    assert TrafficHourly.objects.filter(hour__gte=datetime(2026, 11, 5, tzinfo=UTC)).count() == 5


def test_folding_twice_changes_nothing() -> None:
    hours(key_of(), datetime(2026, 11, 1, tzinfo=UTC), 24)
    rollup = TrafficRollup(KEEP, datetime(2026, 11, 5, tzinfo=UTC))
    rollup.run()

    assert rollup.run() == 0


def test_a_day_with_a_period_start_is_cut_there_and_limits_count_the_same() -> None:
    assigned = datetime(2026, 9, 10, 12, 30, tzinfo=UTC)
    limits: UserLimits = UserLimitsFactory(
        plan=PlanFactory(period_days=30), plan_assigned_at=assigned
    )
    key = key_of(limits.user)
    hours(key, datetime(2026, 9, 9, tzinfo=UTC), 24 * 35)
    now = datetime(2026, 10, 13, 8, tzinfo=UTC)
    effective = EffectiveLimits.of(limits)
    before = LimitEnforcer(now).usage(limits.user, effective)

    TrafficRollup(KEEP, now).run()

    assert LimitEnforcer(now).usage(limits.user, effective) == before
    folded = TrafficHourly.objects.filter(hour__date=datetime(2026, 10, 10).date())
    assert list(folded.order_by("hour").values_list("hour", flat=True)) == [
        datetime(2026, 10, 10, tzinfo=UTC),
        datetime(2026, 10, 10, 13, tzinfo=UTC),
    ]


@override_settings(TIME_ZONE="Europe/Moscow")
def test_days_are_local() -> None:
    key = key_of()
    # 21:00 UTC is midnight in Moscow: the first hour of the next local day.
    hours(key, datetime(2026, 10, 31, 21, tzinfo=UTC), 24)

    TrafficRollup(KEEP, datetime(2026, 11, 5, tzinfo=UTC)).run()

    assert list(TrafficHourly.objects.values_list("hour", flat=True)) == [
        datetime(2026, 10, 31, 21, tzinfo=UTC)
    ]


def test_traffic_of_users_without_a_plan_is_folded_too() -> None:
    hours(key_of(UserFactory()), datetime(2026, 11, 1, tzinfo=UTC), 24)

    TrafficRollup(KEEP, datetime(2026, 11, 5, tzinfo=UTC)).run()

    assert TrafficHourly.objects.count() == 1
