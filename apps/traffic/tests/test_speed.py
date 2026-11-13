from collections.abc import Callable
from contextlib import AbstractContextManager
from ipaddress import IPv4Address
from typing import Any

import pytest
from django.utils import timezone

from amnezia_core.start_script import START_SCRIPT_PATH
from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.actions import KeyActions
from apps.keys.tests.conftest import SOURCE
from apps.keys.tests.fake_server import FakeAwgServer
from apps.operations.models import Operation, OperationStatus
from apps.plans.models import UserLimits
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory
from apps.traffic.models import TrafficSource
from apps.traffic.services.shaping import SpeedRule
from apps.traffic.services.speed import SPEED_APPLY, SpeedLimiter

pytestmark = pytest.mark.django_db

OnCommit = Callable[..., AbstractContextManager[Any]]
SPEED_SCRIPT = "/opt/amnezia/awg/lamnezia_speed.sh"
HOOK = f"[ -f {SPEED_SCRIPT} ] && sh {SPEED_SCRIPT} || true"
UPSTREAM_START = "#!/bin/bash\nawg-quick up /opt/amnezia/awg/awg0.conf\ntail -f /dev/null\n"


@pytest.fixture
def admin() -> KeyActions:
    return KeyActions(UserFactory(role=Role.ADMIN))


@pytest.fixture
def server(awg_server: FakeAwgServer) -> FakeAwgServer:
    awg_server.files[START_SCRIPT_PATH] = UPSTREAM_START
    return awg_server


@pytest.fixture
def sync(django_capture_on_commit_callbacks: OnCommit) -> Callable[[], list[Operation]]:
    """Compare the rules and run the started operations at once, as the worker would."""

    def run() -> list[Operation]:
        with django_capture_on_commit_callbacks(execute=True):
            started = SpeedLimiter().sync_all()
        for operation in started:
            operation.refresh_from_db()
            assert operation.status == OperationStatus.SUCCEEDED
        return started

    return run


def subscriber(speed: int | None = None, **personal: Any) -> User:
    limits: UserLimits = UserLimitsFactory(plan=PlanFactory(speed_limit=speed), **personal)
    return limits.user


def issued(admin: KeyActions, owner: User | None, capture: OnCommit) -> AccessKey:
    with capture(execute=True):
        admin.issue(SOURCE, "iPhone", owner)
    return AccessKey.objects.latest("pk")


def rule(key: AccessKey, mbit: int) -> SpeedRule:
    return SpeedRule(IPv4Address(key.client_ip), mbit)


def test_plan_speed_applies_to_active_keys_of_its_users(
    server: FakeAwgServer, admin: KeyActions, django_capture_on_commit_callbacks: OnCommit
) -> None:
    limited = issued(admin, subscriber(speed=10), django_capture_on_commit_callbacks)
    personal = issued(
        admin, subscriber(speed=10, speed_limit=50), django_capture_on_commit_callbacks
    )
    issued(admin, subscriber(), django_capture_on_commit_callbacks)
    issued(admin, None, django_capture_on_commit_callbacks)
    disabled = issued(admin, subscriber(speed=5), django_capture_on_commit_callbacks)
    AccessKey.objects.filter(pk=disabled.pk).update(status=KeyStatus.DISABLED_MANUAL)

    expected = sorted([rule(limited, 10), rule(personal, 50)])
    assert SpeedLimiter.wanted_rules()[SOURCE] == [r.to_json() for r in expected]


def test_limit_is_written_into_the_container_and_its_start_script(
    server: FakeAwgServer,
    admin: KeyActions,
    django_capture_on_commit_callbacks: OnCommit,
    sync: Callable[[], list[Operation]],
) -> None:
    key = issued(admin, subscriber(speed=10), django_capture_on_commit_callbacks)

    (operation,) = sync()

    assert operation.kind == SPEED_APPLY
    assert operation.params == {
        "source": SOURCE,
        "rules": [rule(key, 10).to_json()],
        "started": "",
    }
    assert f"match ip dst {key.client_ip}/32" in server.files[SPEED_SCRIPT]
    assert any(f"sh {SPEED_SCRIPT}" in command for command in server.commands)
    assert server.files[START_SCRIPT_PATH].splitlines()[-2:] == [HOOK, "tail -f /dev/null"]


def test_unchanged_limits_start_nothing(
    server: FakeAwgServer,
    admin: KeyActions,
    django_capture_on_commit_callbacks: OnCommit,
    sync: Callable[[], list[Operation]],
) -> None:
    issued(admin, subscriber(speed=10), django_capture_on_commit_callbacks)
    sync()
    hooked = server.files[START_SCRIPT_PATH]

    assert sync() == []
    assert server.files[START_SCRIPT_PATH] == hooked
    assert hooked.count(HOOK) == 1


def polled(started: str) -> None:
    """The poll found the container started at `started`."""
    TrafficSource.objects.update_or_create(
        source=SOURCE, defaults={"started": started, "checked_at": timezone.now()}
    )


def test_a_container_started_anew_gets_its_limits_back(
    server: FakeAwgServer,
    admin: KeyActions,
    django_capture_on_commit_callbacks: OnCommit,
    sync: Callable[[], list[Operation]],
) -> None:
    key = issued(admin, subscriber(speed=10), django_capture_on_commit_callbacks)
    polled("2026-11-12T08:00:00Z")
    sync()
    assert sync() == []

    # The app re-created the container: the tc rules and their script are gone.
    del server.files[SPEED_SCRIPT]
    server.files[START_SCRIPT_PATH] = UPSTREAM_START
    polled("2026-11-12T09:30:00Z")
    (operation,) = sync()

    assert operation.params["started"] == "2026-11-12T09:30:00Z"
    assert f"match ip dst {key.client_ip}/32" in server.files[SPEED_SCRIPT]
    assert HOOK in server.files[START_SCRIPT_PATH]
    assert sync() == []


def test_a_new_start_without_limits_starts_nothing(
    server: FakeAwgServer, sync: Callable[[], list[Operation]]
) -> None:
    polled("2026-11-12T09:30:00Z")

    assert sync() == []


def test_a_new_speed_replaces_the_rules_without_a_restart(
    server: FakeAwgServer,
    admin: KeyActions,
    django_capture_on_commit_callbacks: OnCommit,
    sync: Callable[[], list[Operation]],
) -> None:
    user = subscriber(speed=10)
    key = issued(admin, user, django_capture_on_commit_callbacks)
    sync()

    UserLimits.objects.filter(user=user).update(speed_limit=20)
    (operation,) = sync()

    assert operation.params["rules"] == [rule(key, 20).to_json()]
    assert "rate 20mbit" in server.files[SPEED_SCRIPT]
    assert not any("docker restart" in command for command in server.commands)


def test_removed_limit_clears_the_rules(
    server: FakeAwgServer,
    admin: KeyActions,
    django_capture_on_commit_callbacks: OnCommit,
    sync: Callable[[], list[Operation]],
) -> None:
    user = subscriber(speed=10)
    issued(admin, user, django_capture_on_commit_callbacks)
    sync()

    UserLimits.objects.filter(user=user).update(plan=PlanFactory())
    (operation,) = sync()

    assert operation.params["rules"] == []
    assert "htb" not in server.files[SPEED_SCRIPT]
    assert "tc qdisc del" in server.files[SPEED_SCRIPT]


def test_instance_that_never_had_limits_is_left_alone(server: FakeAwgServer) -> None:
    assert SpeedLimiter().sync_all() == []


def test_unfinished_operation_is_waited_for(
    server: FakeAwgServer, admin: KeyActions, django_capture_on_commit_callbacks: OnCommit
) -> None:
    user = subscriber(speed=10)
    issued(admin, user, django_capture_on_commit_callbacks)
    (pending,) = SpeedLimiter().sync_all()  # not run: the commit callbacks are not executed

    UserLimits.objects.filter(user=user).update(speed_limit=20)

    assert SpeedLimiter().sync_all() == []
    assert Operation.objects.get(kind=SPEED_APPLY) == pending


def test_failed_operation_is_not_repeated_by_itself(
    server: FakeAwgServer, admin: KeyActions, django_capture_on_commit_callbacks: OnCommit
) -> None:
    issued(admin, subscriber(speed=10), django_capture_on_commit_callbacks)
    (failed,) = SpeedLimiter().sync_all()
    Operation.objects.filter(pk=failed.pk).update(status=OperationStatus.FAILED)

    assert SpeedLimiter().sync_all() == []


def test_start_script_without_a_place_for_the_hook_is_kept(
    server: FakeAwgServer,
    admin: KeyActions,
    django_capture_on_commit_callbacks: OnCommit,
    sync: Callable[[], list[Operation]],
) -> None:
    server.files[START_SCRIPT_PATH] = "#!/bin/sh\nexec sleep infinity\n"
    issued(admin, subscriber(speed=10), django_capture_on_commit_callbacks)

    (operation,) = sync()

    assert server.files[START_SCRIPT_PATH] == "#!/bin/sh\nexec sleep infinity\n"
    log = "".join(step.log for step in operation.steps.all())
    assert "no place to start the limits from" in log
