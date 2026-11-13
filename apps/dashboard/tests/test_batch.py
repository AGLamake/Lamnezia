from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from amnezia_core.awg import AWG_CONFIG_PATH, AwgServerConfig
from apps.accounts.models import Role, Status, User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.dashboard.models import BatchAction, Outcome, UserBatch
from apps.dashboard.services.batch import BatchReport, UserBatchActions
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.actions import DISABLE, KeyActions
from apps.keys.services.limits import LIMIT_ENABLE
from apps.keys.services.reissue import REISSUE
from apps.keys.tests.conftest import SOURCE
from apps.keys.tests.fake_server import FakeAwgServer
from apps.operations.models import OperationStatus
from apps.plans.models import UserLimits
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory
from apps.traffic.services.limits import LimitEnforcer

pytestmark = pytest.mark.django_db

OnCommit = Callable[..., AbstractContextManager[Any]]
ASSIGNED = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)


@pytest.fixture
def admin() -> User:
    return UserFactory(role=Role.ADMIN)


def actions(admin: User, now: datetime) -> UserBatchActions:
    return UserBatchActions(admin, ip="203.0.113.5", clock=lambda: now)


def subscriber(**plan: Any) -> User:
    limits: UserLimits = UserLimitsFactory(plan=PlanFactory(**plan), plan_assigned_at=ASSIGNED)
    return limits.user


def issued(admin: User, owner: User, capture: OnCommit, name: str = "iPhone") -> AccessKey:
    with capture(execute=True):
        KeyActions(admin).issue(SOURCE, name, owner)
    return AccessKey.objects.latest("pk")


def peers_of(server: FakeAwgServer) -> set[str]:
    return {p.public_key for p in AwgServerConfig.parse(server.files[AWG_CONFIG_PATH]).peers}


def status_of(key: AccessKey) -> str:
    key.refresh_from_db()
    return key.status


def access_until(user: User) -> datetime | None:
    return UserLimits.objects.get(user=user).access_until


def test_extend_restores_expired_access_with_key_operations(
    awg_server: FakeAwgServer, admin: User, django_capture_on_commit_callbacks: OnCommit
) -> None:
    user = subscriber(access_days=7)
    key = issued(admin, user, django_capture_on_commit_callbacks)
    with django_capture_on_commit_callbacks(execute=True):
        LimitEnforcer(ASSIGNED + 8 * DAY).enforce_all()
    assert status_of(key) == KeyStatus.DISABLED_LIMIT

    now = ASSIGNED + 10 * DAY
    with django_capture_on_commit_callbacks(execute=True):
        batch = actions(admin, now).extend(User.objects.filter(pk=user.pk), 30)

    assert access_until(user) == now + 30 * DAY
    assert status_of(key) == KeyStatus.ACTIVE
    assert key.public_key in peers_of(awg_server)
    report = BatchReport.of(batch)
    assert (report.done, report.skipped, report.succeeded, report.is_finished) == (1, 0, 1, True)
    assert [op.kind for op in report.operations] == [LIMIT_ENABLE]


def test_extend_adds_days_to_a_term_not_yet_over(admin: User) -> None:
    user = subscriber(access_days=30)

    actions(admin, ASSIGNED + DAY).extend([user], 10)

    assert access_until(user) == ASSIGNED + 40 * DAY


def test_extend_skips_users_without_a_plan_or_an_end(admin: User) -> None:
    no_plan = UserFactory()
    endless = subscriber()

    batch = actions(admin, ASSIGNED).extend([no_plan, endless], 10)

    items = list(batch.items.all())
    assert [item.outcome for item in items] == [Outcome.SKIPPED, Outcome.SKIPPED]
    assert [item.note for item in items] == ["No plan.", "Access has no end date."]
    assert access_until(endless) is None


def test_disable_turns_off_keys_by_hand(
    awg_server: FakeAwgServer, admin: User, django_capture_on_commit_callbacks: OnCommit
) -> None:
    user = subscriber()
    phone = issued(admin, user, django_capture_on_commit_callbacks)
    laptop = issued(admin, user, django_capture_on_commit_callbacks, "Laptop")
    AccessKey.objects.filter(pk=laptop.pk).update(status=KeyStatus.DISABLED_LIMIT)
    idle = UserFactory()

    with django_capture_on_commit_callbacks(execute=True):
        batch = actions(admin, ASSIGNED).disable([user, idle])

    assert status_of(phone) == status_of(laptop) == KeyStatus.DISABLED_MANUAL
    assert phone.public_key not in peers_of(awg_server)
    report = BatchReport.of(batch)
    assert [item.outcome for item in report.items] == [Outcome.DONE, Outcome.SKIPPED]
    assert [op.kind for op in report.operations] == [DISABLE]
    assert report.succeeded == 1


def test_reissue_shares_one_operation_per_instance_among_the_users(
    awg_server: FakeAwgServer, admin: User, django_capture_on_commit_callbacks: OnCommit
) -> None:
    first, second = subscriber(), subscriber()
    olds = [
        issued(admin, first, django_capture_on_commit_callbacks),
        issued(admin, first, django_capture_on_commit_callbacks, "Laptop"),
        issued(admin, second, django_capture_on_commit_callbacks),
    ]
    blocked = subscriber()
    issued(admin, blocked, django_capture_on_commit_callbacks)
    User.objects.filter(pk=blocked.pk).update(status=Status.BLOCKED)
    idle = UserFactory()

    with django_capture_on_commit_callbacks(execute=True):
        batch = actions(admin, ASSIGNED).reissue(
            User.objects.filter(pk__in=[first.pk, second.pk, blocked.pk, idle.pk]).order_by("pk")
        )

    assert [status_of(old) for old in olds] == [KeyStatus.REVOKED] * 3
    assert AccessKey.objects.filter(status=KeyStatus.ACTIVE).count() == 4
    report = BatchReport.of(batch)
    assert [item.note for item in report.items] == [
        "Keys to reissue: 2.",
        "Keys to reissue: 1.",
        "The user may not have keys.",
        "No keys.",
    ]
    assert [op.kind for op in report.operations] == [REISSUE]
    assert report.succeeded == 1


def test_change_plan_starts_the_term_again_and_skips_the_same_plan(admin: User) -> None:
    plan = PlanFactory(access_days=30)
    moved = subscriber()
    staying: UserLimits = UserLimitsFactory(plan=plan, plan_assigned_at=ASSIGNED)
    now = ASSIGNED + 5 * DAY

    batch = actions(admin, now).change_plan([moved, staying.user], plan)

    limits = UserLimits.objects.get(user=moved)
    assert (limits.plan, limits.plan_assigned_at) == (plan, now)
    assert [item.outcome for item in batch.items.all()] == [Outcome.DONE, Outcome.SKIPPED]
    assert batch.items.first().note == "Access until 12/06/2026."  # type: ignore[union-attr]


def test_every_user_is_written_to_the_audit_journal(admin: User) -> None:
    users: list[User] = [subscriber(access_days=30), UserFactory()]

    batch = actions(admin, ASSIGNED).extend(users, 7)

    events = AuditEvent.objects.filter(action="users.extend").order_by("pk")
    assert [event.target_id for event in events] == [str(user.pk) for user in users]
    assert {event.data["batch"] for event in events} == {batch.pk}
    assert {event.actor_id for event in events} == {admin.pk}


def test_report_counts_unfinished_and_failed_operations(
    awg_server: FakeAwgServer, admin: User, django_capture_on_commit_callbacks: OnCommit
) -> None:
    user = subscriber()
    issued(admin, user, django_capture_on_commit_callbacks)
    issued(admin, user, django_capture_on_commit_callbacks, "Laptop")

    batch = actions(admin, ASSIGNED).disable([user])
    report = BatchReport.of(batch)
    assert (report.unfinished, report.is_finished) == (2, False)

    first, second = report.operations
    first.finish(OperationStatus.FAILED, code="x")
    second.finish(OperationStatus.SUCCEEDED)
    report = BatchReport.of(UserBatch.objects.get(pk=batch.pk))
    assert (report.failed, report.succeeded, report.is_finished) == (1, 1, True)
    assert batch.action == BatchAction.DISABLE
