from datetime import UTC, datetime, timedelta

import pytest
from django.test import override_settings

from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.dashboard.services.forecast import LimitForecast
from apps.dashboard.services.stats import (
    Period,
    Scope,
    TrafficStats,
    Window,
    inactive,
    top_users,
)
from apps.keys.models import AccessKey, KeyStatus
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory
from apps.protocols.models import InstanceOrigin, ProtocolInstance
from apps.servers.tests.factories import ServerFactory
from apps.traffic.models import TrafficCounter, TrafficHourly, TrafficSource
from apps.traffic.services.online import OnlineRule

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 11, 2, 12, 30, tzinfo=UTC)
ONLINE = OnlineRule(timedelta(minutes=3), timedelta(minutes=8))
GB = 1024**3


def key_of(user: User | None, source: str = "instance:1", name: str = "iPhone") -> AccessKey:
    return AccessKey.objects.create(
        user=user,
        source=source,
        name=name,
        public_key=f"key-{AccessKey.objects.count()}",
        status=KeyStatus.ACTIVE,
    )


def traffic(key: AccessKey, at: datetime, rx: int, tx: int = 0) -> None:
    TrafficHourly.objects.create(
        source=key.source,
        public_key=key.public_key,
        hour=at.replace(minute=0),
        rx_bytes=rx,
        tx_bytes=tx,
    )


def stats(scope: Scope | None = None, period: Period = Period.WEEK) -> TrafficStats:
    return TrafficStats(scope or Scope(), Window(period, NOW), ONLINE)


def test_day_is_drawn_in_hours_and_week_in_days() -> None:
    day = Window(Period.DAY, NOW).buckets()
    week = Window(Period.WEEK, NOW).buckets()

    assert len(day) == 24
    assert day[-1] == datetime(2026, 11, 2, 12, tzinfo=UTC)
    assert day[0] == datetime(2026, 11, 1, 13, tzinfo=UTC)
    assert len(week) == 7
    assert week[0] == datetime(2026, 10, 27, tzinfo=UTC)
    assert len(Window(Period.MONTH, NOW).buckets()) == 30


@override_settings(TIME_ZONE="Europe/Moscow")
def test_days_follow_the_local_time() -> None:
    key = key_of(UserFactory())
    traffic(key, datetime(2026, 11, 1, 22, tzinfo=UTC), rx=5)  # 01:00 on Nov 2 in Moscow

    series = stats().series()

    assert series[-1].at.date().isoformat() == "2026-11-02"
    assert series[-1].rx == 5


def test_series_sums_traffic_per_bucket_and_keeps_empty_ones() -> None:
    alice, bob = key_of(UserFactory()), key_of(UserFactory())
    traffic(alice, NOW - timedelta(hours=1), rx=10, tx=100)
    traffic(bob, NOW - timedelta(hours=2), rx=1, tx=2)
    traffic(alice, NOW - timedelta(days=3), rx=7)
    traffic(alice, NOW - timedelta(days=8), rx=1000)

    series = stats().series()

    assert [(point.rx, point.tx) for point in series] == [
        (0, 0),
        (0, 0),
        (0, 0),
        (7, 0),
        (0, 0),
        (0, 0),
        (11, 102),
    ]


def test_scope_narrows_to_a_server_a_user_or_a_key() -> None:
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
    alice = UserFactory()
    on_server, elsewhere = key_of(alice, instance.source), key_of(alice, "instance:other")
    other = key_of(UserFactory(), instance.source)
    for amount, key in enumerate((on_server, elsewhere, other), start=1):
        traffic(key, NOW, rx=amount)

    def total(scope: Scope) -> int:
        return sum(point.rx for point in stats(scope).series())

    assert total(Scope()) == 6
    assert total(Scope(server=server)) == 4
    assert total(Scope(user=alice)) == 3
    assert total(Scope(server=server, user=alice)) == 1
    assert total(Scope(key=other)) == 3
    assert {row.key for row in stats(Scope(server=server)).by_key()} == {on_server, other}


def test_top_users_add_up_their_keys_and_skip_ownerless_ones() -> None:
    alice, bob = UserFactory(), UserFactory()
    phone, laptop, tablet = key_of(alice), key_of(alice), key_of(bob)
    traffic(phone, NOW, rx=3)
    traffic(laptop, NOW, rx=3)
    traffic(tablet, NOW, rx=5)
    traffic(key_of(None), NOW, rx=100)

    top = top_users(stats().by_key())

    assert [(row.user, row.total) for row in top] == [(alice, 6), (bob, 5)]


def test_inactive_are_active_keys_without_traffic_in_the_period() -> None:
    user = UserFactory()
    busy, idle, old = key_of(user, name="busy"), key_of(user, name="idle"), key_of(user, name="old")
    off = key_of(user, name="off")
    AccessKey.objects.filter(pk=off.pk).update(status=KeyStatus.DISABLED_MANUAL)
    traffic(busy, NOW, rx=1)
    traffic(old, NOW - timedelta(days=10), rx=1)
    TrafficSource.objects.create(source="instance:1", checked_at=NOW)

    assert inactive(stats().by_key()) == [idle, old]


def test_keys_of_a_protocol_without_counters_are_not_inactive() -> None:
    # OpenVPN and IPsec give no counters: their source is never read.
    key_of(UserFactory(), source="instance:9")

    rows = stats().by_key()

    assert not rows[0].has_data
    assert inactive(rows) == []


def test_online_keys_have_a_recent_handshake() -> None:
    user = UserFactory()
    recent, stale = key_of(user, name="recent"), key_of(user, name="stale")
    for key, ago in ((recent, 1), (stale, 10)):
        TrafficCounter.objects.create(
            source=key.source,
            public_key=key.public_key,
            latest_handshake=NOW - timedelta(minutes=ago),
            collected_at=NOW,
        )

    assert stats().online() == [recent]


def test_online_keys_include_xray_with_recent_traffic() -> None:
    user = UserFactory()
    busy, quiet, gone = (key_of(user, name=name) for name in ("busy", "quiet", "gone"))
    for key, ago, present in ((busy, 6, True), (quiet, 10, True), (gone, 1, False)):
        TrafficCounter.objects.create(
            source=key.source,
            public_key=key.public_key,
            active_at=NOW - timedelta(minutes=ago),
            present=present,
            collected_at=NOW,
        )

    assert stats().online() == [busy]


class TestForecast:
    def subscribe(self, limit: int, assigned_days_ago: int, period_days: int = 30) -> User:
        user = UserFactory()
        UserLimitsFactory(
            user=user,
            plan=PlanFactory(traffic_limit=limit, period_days=period_days),
            plan_assigned_at=NOW - timedelta(days=assigned_days_ago),
        )
        return user

    def soonest(self) -> list[tuple[User, int, datetime]]:
        return [
            (row.user, row.used, row.at) for row in LimitForecast(NOW).soonest(User.objects.all())
        ]

    def test_pace_of_the_period_gives_the_date(self) -> None:
        user = self.subscribe(limit=10 * GB, assigned_days_ago=5)
        traffic(key_of(user), NOW - timedelta(days=2), rx=4 * GB, tx=1 * GB)

        assert self.soonest() == [(user, 5 * GB, NOW + timedelta(days=5))]

    def test_users_who_last_the_period_are_left_out(self) -> None:
        user = self.subscribe(limit=10 * GB, assigned_days_ago=20)
        traffic(key_of(user), NOW - timedelta(days=1), rx=1 * GB)

        assert self.soonest() == []

    def test_reached_limits_come_first(self) -> None:
        slow = self.subscribe(limit=10 * GB, assigned_days_ago=5)
        traffic(key_of(slow), NOW - timedelta(days=1), rx=5 * GB)
        done = self.subscribe(limit=1 * GB, assigned_days_ago=5)
        traffic(key_of(done), NOW - timedelta(days=1), rx=2 * GB)

        forecast = LimitForecast(NOW).soonest(User.objects.all())

        assert [row.user for row in forecast] == [done, slow]
        assert forecast[0].reached

    def test_traffic_before_the_current_period_does_not_count(self) -> None:
        user = self.subscribe(limit=10 * GB, assigned_days_ago=35)
        key = key_of(user)
        traffic(key, NOW - timedelta(days=8), rx=9 * GB)
        traffic(key, NOW - timedelta(days=1), rx=4 * GB)

        assert self.soonest() == [(user, 4 * GB, NOW + timedelta(days=7.5))]

    def test_unlimited_traffic_has_no_forecast(self) -> None:
        user = UserFactory()
        UserLimitsFactory(user=user, plan=PlanFactory(), plan_assigned_at=NOW - timedelta(days=5))
        traffic(key_of(user), NOW, rx=100 * GB)

        assert self.soonest() == []


def test_keys_of_a_source_without_readings_are_unknown_not_inactive() -> None:
    user = UserFactory()
    awg, xray = key_of(user, name="awg"), key_of(user, source="instance:2", name="xray")
    TrafficSource.objects.create(source="instance:1", available=True, checked_at=NOW)
    TrafficSource.objects.create(source="instance:2", available=False, checked_at=NOW)
    TrafficSource.objects.create(source="instance:3", available=False, checked_at=NOW)

    assert inactive(stats().by_key()) == [awg]
    assert xray not in inactive(stats().by_key())
