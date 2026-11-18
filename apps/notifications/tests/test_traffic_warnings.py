"""Users hear once a period that most of their traffic limit is used (`traffic_low`)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.utils import translation

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.core.models import SiteSettings
from apps.core.services.site_settings import cached_settings
from apps.keys.models import AccessKey, KeyStatus
from apps.notifications.events import EVENTS, Event
from apps.notifications.models import Notification
from apps.notifications.services.reminders import TrafficWarnings
from apps.plans.models import UserLimits
from apps.plans.services.renewal import renew
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory
from apps.traffic.models import TrafficHourly, TrafficSource

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("commit")]

GB = 1024**3
NOW = datetime(2026, 11, 18, 12, 30, tzinfo=UTC)
DAY = timedelta(days=1)


def subscriber(assigned_days_ago: int = 5, **plan: Any) -> User:
    """A user on a plan with 30-day periods."""
    limits: UserLimits = UserLimitsFactory(
        plan=PlanFactory(period_days=30, **plan), plan_assigned_at=NOW - assigned_days_ago * DAY
    )
    return limits.user


def used(
    user: User, gigabytes: float, at: datetime = NOW - DAY, source: str = "instance:1"
) -> None:
    key = AccessKey.objects.create(
        user=user,
        source=source,
        name="iPhone",
        public_key=f"key-{AccessKey.objects.count()}",
        status=KeyStatus.ACTIVE,
    )
    TrafficHourly.objects.create(
        source=source,
        public_key=key.public_key,
        hour=at.replace(minute=0),
        rx_bytes=int(gigabytes * GB),
        tx_bytes=0,
    )


def warn(now: datetime = NOW, percent: int = 80) -> list[dict[str, Any]]:
    """Run the warnings; return the details of every `traffic_low` so far."""
    TrafficWarnings(now, percent).warn_all()
    return [n.data for n in Notification.objects.filter(event=Event.TRAFFIC_LOW).order_by("pk")]


def test_a_user_past_the_threshold_is_warned_once() -> None:
    user = subscriber(traffic_limit=10 * GB)
    used(user, 8.5)

    warn()
    [data] = warn(NOW + timedelta(hours=1))

    assert data["user"] == user.username
    assert (data["percent"], data["limit"], data["incomplete"]) == (85, "of the period", False)
    assert data["url"].endswith("/usage/")


def test_users_below_the_threshold_or_without_a_limit_are_not_warned() -> None:
    used(subscriber(traffic_limit=10 * GB), 7)
    used(subscriber(), 100)

    assert warn() == []


def test_a_reached_limit_is_left_to_limit_reached() -> None:
    used(subscriber(traffic_limit=10 * GB), 10)

    assert warn() == []


def test_the_threshold_comes_from_the_settings_and_zero_turns_it_off() -> None:
    used(subscriber(traffic_limit=10 * GB), 6)
    site = SiteSettings.load()
    site.traffic_warning_percent = 0
    site.save()
    cached_settings.invalidate()

    TrafficWarnings.from_settings(NOW).warn_all()
    assert not Notification.objects.exists()

    site.traffic_warning_percent = 50
    site.save()
    cached_settings.invalidate()
    TrafficWarnings.from_settings(NOW).warn_all()
    assert Notification.objects.get().data["percent"] == 60


def test_a_new_period_warns_again() -> None:
    user = subscriber(traffic_limit=10 * GB, assigned_days_ago=29)
    used(user, 9)
    warn()

    used(user, 9, at=NOW + 2 * DAY)

    assert len(warn(NOW + 3 * DAY)) == 2


def test_a_plan_bought_anew_starts_a_period_and_warns_again() -> None:
    user = subscriber(traffic_limit=10 * GB)
    used(user, 9)
    warn()

    renew(user, PlanFactory(period_days=30, traffic_limit=10 * GB), 30, NOW + DAY)
    assert len(warn(NOW + 2 * DAY)) == 1
    used(user, 9, at=NOW + 2 * DAY)

    assert len(warn(NOW + 3 * DAY)) == 2


def test_an_extension_of_the_same_plan_keeps_the_period_and_the_warning() -> None:
    plan = PlanFactory(period_days=30, traffic_limit=10 * GB)
    user = UserLimitsFactory(plan=plan, plan_assigned_at=NOW - 5 * DAY).user
    used(user, 9)
    warn()

    renew(user, plan, 30, NOW + DAY)

    assert len(warn(NOW + 2 * DAY)) == 1


def test_a_raised_personal_limit_warns_again_when_crossed() -> None:
    user = subscriber(traffic_limit=10 * GB)
    used(user, 9)
    warn()

    UserLimits.objects.filter(user=user).update(traffic_limit=20 * GB)
    assert len(warn()) == 1
    used(user, 8)

    assert [data["percent"] for data in warn()] == [90, 85]


def test_the_limit_of_the_whole_plan_is_warned_of_on_its_own() -> None:
    user = subscriber(total_traffic_limit=100 * GB, assigned_days_ago=60)
    used(user, 50, at=NOW - 50 * DAY)
    used(user, 35)

    [data] = warn()

    assert (data["percent"], data["limit"]) == (85, "of the plan")


def test_an_expired_access_is_not_warned_of() -> None:
    user = subscriber(traffic_limit=10 * GB)
    UserLimits.objects.filter(user=user).update(access_until=NOW - DAY)
    used(user, 9)

    assert warn() == []


def test_unknown_traffic_of_a_key_is_noted() -> None:
    user = subscriber(traffic_limit=10 * GB)
    used(user, 9)
    used(user, 0, source="instance:2")
    TrafficSource.objects.create(source="instance:2", available=False, checked_at=NOW)

    [data] = warn()

    assert data["incomplete"] is True
    assert "not counted" in EVENTS[Event.TRAFFIC_LOW].render(data)[1]


def test_the_text_without_a_note_ends_with_the_link() -> None:
    used(subscriber(traffic_limit=10 * GB), 9)
    [data] = warn()

    title, text = EVENTS[Event.TRAFFIC_LOW].render(data)

    assert title == "90% of the traffic is used"
    assert text.splitlines() == [
        f"{data['user']} has used 90% of the traffic limit of the period.",
        data["url"],
    ]


def test_inactive_users_are_not_warned() -> None:
    user = subscriber(traffic_limit=10 * GB)
    used(user, 9)
    User.objects.filter(pk=user.pk).update(is_active=False)
    UserFactory()

    assert warn() == []


def test_the_text_is_in_the_language_of_the_reader() -> None:
    used(subscriber(traffic_limit=10 * GB), 9)
    [data] = warn()

    with translation.override("ru"):
        title, text = EVENTS[Event.TRAFFIC_LOW].render(data)

    assert title == "Израсходовано 90% трафика"
    assert "лимита трафика на период" in text
