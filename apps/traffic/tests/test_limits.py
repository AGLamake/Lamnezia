from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from amnezia_core.awg import AWG_CONFIG_PATH, AwgServerConfig
from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.actions import KeyActions
from apps.keys.services.backends import WgKeys
from apps.keys.services.limits import LIMIT_DISABLE, LIMIT_ENABLE
from apps.keys.tests.conftest import SOURCE
from apps.keys.tests.fake_server import FakeAwgServer
from apps.operations.models import Operation, OperationStatus
from apps.plans.models import UserLimits
from apps.plans.services.limits import effective_limits
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory
from apps.traffic.models import TrafficHourly
from apps.traffic.services.limits import LimitEnforcer
from apps.traffic.signals import LimitReason, access_restored, limit_reached

pytestmark = pytest.mark.django_db

OnCommit = Callable[..., AbstractContextManager[Any]]
GB = 1024**3
ASSIGNED = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)


@pytest.fixture
def admin() -> KeyActions:
    return KeyActions(UserFactory(role=Role.ADMIN))


@pytest.fixture
def enforce(django_capture_on_commit_callbacks: OnCommit) -> Callable[[datetime], None]:
    """Apply the limits and run the started operations at once, as the worker would."""

    def run(now: datetime) -> None:
        with django_capture_on_commit_callbacks(execute=True):
            LimitEnforcer(now).enforce_all()
        assert not Operation.objects.exclude(status=OperationStatus.SUCCEEDED).exists()

    return run


@pytest.fixture
def events() -> Iterator[list[tuple[str, dict[str, Any]]]]:
    received: list[tuple[str, dict[str, Any]]] = []

    def on_reached(**kwargs: Any) -> None:
        received.append(("reached", kwargs))

    def on_restored(**kwargs: Any) -> None:
        received.append(("restored", kwargs))

    limit_reached.connect(on_reached)
    access_restored.connect(on_restored)
    yield received
    limit_reached.disconnect(on_reached)
    access_restored.disconnect(on_restored)


def subscriber(**plan: Any) -> User:
    """A user on a plan with 30-day periods, assigned at ASSIGNED."""
    limits: UserLimits = UserLimitsFactory(
        plan=PlanFactory(period_days=30, **plan), plan_assigned_at=ASSIGNED
    )
    return limits.user


def issued(
    admin: KeyActions, owner: User | None, capture: OnCommit, name: str = "iPhone"
) -> AccessKey:
    with capture(execute=True):
        admin.issue(SOURCE, name, owner)
    return AccessKey.objects.latest("pk")


def used(key: AccessKey, gigabytes: float, at: datetime) -> None:
    TrafficHourly.objects.create(
        source=key.source,
        public_key=key.public_key,
        hour=at,
        rx_bytes=int(gigabytes * GB) // 2,
        tx_bytes=int(gigabytes * GB) - int(gigabytes * GB) // 2,
    )


def peers_of(server: FakeAwgServer) -> set[str]:
    return {p.public_key for p in AwgServerConfig.parse(server.files[AWG_CONFIG_PATH]).peers}


def status_of(key: AccessKey) -> str:
    key.refresh_from_db()
    return key.status


def test_traffic_over_the_period_limit_turns_the_keys_off(
    awg_server: FakeAwgServer,
    admin: KeyActions,
    django_capture_on_commit_callbacks: OnCommit,
    enforce: Callable[[datetime], None],
    events: list[tuple[str, dict[str, Any]]],
) -> None:
    user = subscriber(traffic_limit=10 * GB)
    phone = issued(admin, user, django_capture_on_commit_callbacks)
    laptop = issued(admin, user, django_capture_on_commit_callbacks, "Laptop")
    used(phone, 6, ASSIGNED + timedelta(hours=1))
    used(laptop, 4, ASSIGNED + timedelta(hours=2))

    enforce(ASSIGNED + DAY)

    assert status_of(phone) == status_of(laptop) == KeyStatus.DISABLED_LIMIT
    assert not {phone.public_key, laptop.public_key} & peers_of(awg_server)
    assert events == [
        (
            "reached",
            {
                "signal": limit_reached,
                "sender": LimitEnforcer,
                "user": user,
                "reason": LimitReason.TRAFFIC,
            },
        )
    ]
    audit = AuditEvent.objects.filter(action=LIMIT_DISABLE)
    assert audit.count() == 2
    assert {event.actor_id for event in audit} == {None}


def test_traffic_below_the_limit_changes_nothing(
    awg_server: FakeAwgServer,
    admin: KeyActions,
    django_capture_on_commit_callbacks: OnCommit,
    enforce: Callable[[datetime], None],
    events: list[tuple[str, dict[str, Any]]],
) -> None:
    user = subscriber(traffic_limit=10 * GB)
    key = issued(admin, user, django_capture_on_commit_callbacks)
    used(key, 9, ASSIGNED + timedelta(hours=1))

    enforce(ASSIGNED + DAY)

    assert status_of(key) == KeyStatus.ACTIVE
    assert key.public_key in peers_of(awg_server)
    assert events == []


def test_new_period_turns_the_keys_back_on(
    awg_server: FakeAwgServer,
    admin: KeyActions,
    django_capture_on_commit_callbacks: OnCommit,
    enforce: Callable[[datetime], None],
    events: list[tuple[str, dict[str, Any]]],
) -> None:
    user = subscriber(traffic_limit=10 * GB)
    key = issued(admin, user, django_capture_on_commit_callbacks)
    used(key, 12, ASSIGNED + timedelta(hours=1))
    enforce(ASSIGNED + DAY)

    enforce(ASSIGNED + 30 * DAY + timedelta(minutes=5))

    assert status_of(key) == KeyStatus.ACTIVE
    assert key.public_key in peers_of(awg_server)
    assert [name for name, _data in events] == ["reached", "restored"]


def test_total_traffic_stays_reached_in_a_new_period(
    awg_server: FakeAwgServer,
    admin: KeyActions,
    django_capture_on_commit_callbacks: OnCommit,
    enforce: Callable[[datetime], None],
) -> None:
    user = subscriber(traffic_limit=10 * GB, total_traffic_limit=15 * GB)
    key = issued(admin, user, django_capture_on_commit_callbacks)
    used(key, 9, ASSIGNED + timedelta(hours=1))
    used(key, 7, ASSIGNED + 31 * DAY)

    enforce(ASSIGNED + 32 * DAY)

    assert status_of(key) == KeyStatus.DISABLED_LIMIT
    assert AuditEvent.objects.get(action=LIMIT_DISABLE).data["reason"] == "total_traffic"


def test_expired_access_turns_off_and_extension_turns_on(
    awg_server: FakeAwgServer,
    admin: KeyActions,
    django_capture_on_commit_callbacks: OnCommit,
    enforce: Callable[[datetime], None],
) -> None:
    user = subscriber(access_days=7)
    key = issued(admin, user, django_capture_on_commit_callbacks)

    enforce(ASSIGNED + 8 * DAY)
    assert status_of(key) == KeyStatus.DISABLED_LIMIT
    assert key.public_key not in peers_of(awg_server)

    UserLimits.objects.filter(user=user).update(access_until=ASSIGNED + 60 * DAY)
    enforce(ASSIGNED + 8 * DAY + timedelta(minutes=5))

    assert status_of(key) == KeyStatus.ACTIVE
    assert key.public_key in peers_of(awg_server)


def test_key_disabled_by_hand_is_not_turned_on(
    awg_server: FakeAwgServer,
    admin: KeyActions,
    django_capture_on_commit_callbacks: OnCommit,
    enforce: Callable[[datetime], None],
) -> None:
    user = subscriber(traffic_limit=10 * GB)
    phone = issued(admin, user, django_capture_on_commit_callbacks)
    laptop = issued(admin, user, django_capture_on_commit_callbacks, "Laptop")
    with django_capture_on_commit_callbacks(execute=True):
        admin.disable(laptop)
    used(phone, 12, ASSIGNED + timedelta(hours=1))
    enforce(ASSIGNED + DAY)

    enforce(ASSIGNED + 31 * DAY)

    assert status_of(phone) == KeyStatus.ACTIVE
    assert status_of(laptop) == KeyStatus.DISABLED_MANUAL
    assert laptop.public_key not in peers_of(awg_server)


def test_overlapping_runs_start_one_operation_per_key(
    awg_server: FakeAwgServer,
    admin: KeyActions,
    django_capture_on_commit_callbacks: OnCommit,
    events: list[tuple[str, dict[str, Any]]],
) -> None:
    user = subscriber(traffic_limit=GB)
    key = issued(admin, user, django_capture_on_commit_callbacks)
    used(key, 2, ASSIGNED + timedelta(hours=1))

    # The worker has not run the first operation yet when the next run comes.
    with django_capture_on_commit_callbacks(execute=False):
        LimitEnforcer(ASSIGNED + DAY).enforce_all()
        LimitEnforcer(ASSIGNED + DAY).enforce_all()

    assert Operation.objects.filter(kind=LIMIT_DISABLE).count() == 1


def test_users_without_a_plan_are_not_checked(
    awg_server: FakeAwgServer,
    admin: KeyActions,
    django_capture_on_commit_callbacks: OnCommit,
    enforce: Callable[[datetime], None],
) -> None:
    key = issued(admin, UserFactory(), django_capture_on_commit_callbacks)
    orphan = issued(admin, None, django_capture_on_commit_callbacks, "Router")

    enforce(ASSIGNED + DAY)

    assert status_of(key) == status_of(orphan) == KeyStatus.ACTIVE


def test_enable_keeps_a_key_turned_off_by_hand_meanwhile(
    awg_server: FakeAwgServer,
    admin: KeyActions,
    django_capture_on_commit_callbacks: OnCommit,
    enforce: Callable[[datetime], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = subscriber(traffic_limit=GB)
    key = issued(admin, user, django_capture_on_commit_callbacks)
    used(key, 2, ASSIGNED + timedelta(hours=1))
    enforce(ASSIGNED + DAY)
    put_back = WgKeys.enable

    def disabled_by_hand_meanwhile(*args: Any) -> None:
        put_back(*args)
        AccessKey.objects.filter(pk=key.pk).update(status=KeyStatus.DISABLED_MANUAL)

    monkeypatch.setattr(WgKeys, "enable", disabled_by_hand_meanwhile)
    enforce(ASSIGNED + 31 * DAY)

    assert Operation.objects.filter(kind=LIMIT_ENABLE).count() == 1
    assert status_of(key) == KeyStatus.DISABLED_MANUAL
    assert key.public_key not in peers_of(awg_server)


def key_of(user: User) -> AccessKey:
    """A key in the database only: enough for counting, nothing to switch on a server."""
    return AccessKey.objects.create(
        user=user,
        source=SOURCE,
        name="iPhone",
        public_key=f"key-{user.pk}",
        status=KeyStatus.ACTIVE,
    )


def test_enforcement_takes_the_same_queries_for_any_number_of_users() -> None:
    def queries() -> int:
        with CaptureQueriesContext(connection) as captured:
            LimitEnforcer(ASSIGNED + DAY).enforce_all()
        return len(captured)

    def within_limits() -> None:
        used(key_of(subscriber(traffic_limit=10 * GB, total_traffic_limit=50 * GB)), 1, ASSIGNED)

    within_limits()
    one = queries()
    for _ in range(4):
        within_limits()

    assert queries() == one


def test_usage_of_many_users_counts_each_from_its_own_starts() -> None:
    old = subscriber()
    old_key = key_of(old)
    used(old_key, 2, ASSIGNED + timedelta(hours=1))
    used(old_key, 1, ASSIGNED + 30 * DAY + timedelta(hours=1))
    new = UserLimitsFactory(plan=PlanFactory(period_days=30), plan_assigned_at=ASSIGNED + 20 * DAY)
    new_key = key_of(new.user)
    used(new_key, 4, ASSIGNED + timedelta(hours=1))
    used(new_key, 1, ASSIGNED + 21 * DAY)

    usage = LimitEnforcer(ASSIGNED + 31 * DAY).usages(
        {user.pk: effective_limits(user) for user in (old, new.user)}
    )

    assert usage == {old.pk: (GB, 3 * GB), new.user.pk: (GB, GB)}
