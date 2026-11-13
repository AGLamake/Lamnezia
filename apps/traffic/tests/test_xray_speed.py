"""Speed limits of XRay keys: routes in server.json, nft rules and start.sh in the container."""

import json
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, nullcontext
from datetime import datetime, timedelta
from typing import Any

import pytest
from django.utils import timezone

from amnezia_core.start_script import START_SCRIPT_PATH, XRAY_STOP_LINE
from amnezia_core.xray import XRAY, XRAY_CONFIG_PATH, XRAY_OWN_CLIENT_PATH, XRAY_PUBLIC_KEY_PATH
from amnezia_core.xray_stats import STATS_COMMAND
from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.keys.models import AccessKey
from apps.keys.services.actions import KeyActions
from apps.keys.services.instances import Instance, instances
from apps.keys.tests.fake_xray import FakeXrayServer
from apps.keys.tests.steps import FIXTURES
from apps.operations.models import Operation, OperationStatus
from apps.plans.models import UserLimits
from apps.plans.tests.factories import PlanFactory, UserLimitsFactory
from apps.ssh.services import CommandResult
from apps.ssh.tests.fakes import result
from apps.traffic.models import TrafficSource
from apps.traffic.services.speed import SPEED_APPLY, SpeedLimiter
from apps.traffic.services.xray_shaping import XraySpeedRule

pytestmark = pytest.mark.django_db

OnCommit = Callable[..., AbstractContextManager[Any]]
SOURCE = "xray:1"
NFT = "/opt/amnezia/xray/lamnezia_speed.nft"
HOOK = f"[ -f {NFT} ] && nft -f {NFT} || true"
APP_CONFIG = (FIXTURES / "xray_server.json").read_text(encoding="utf-8")
UPSTREAM_START = (
    "#!/bin/bash\n"
    "killall -KILL xray\n"
    "if [ -f /opt/amnezia/xray/server.json ]; then"
    " (xray -config /opt/amnezia/xray/server.json); fi\n"
    "tail -f /dev/null\n"
)


class XrayWithApi(FakeXrayServer):
    """The fake XRay container whose statistics API answers only while `api` is set."""

    api = True

    def handle(self, command: str, stdin: bytes) -> CommandResult:
        if STATS_COMMAND in command and not self.api:
            self.commands.append(command)
            return result(1)
        return super().handle(command, stdin)


@pytest.fixture
def server() -> Iterator[XrayWithApi]:
    server = XrayWithApi(
        {
            XRAY_CONFIG_PATH: APP_CONFIG,
            XRAY_OWN_CLIENT_PATH: "6f0c3e52-1b7a-4c8e-9d2f-0a1b2c3d4e5f\n",
            XRAY_PUBLIC_KEY_PATH: "Wv1cT4hZbYk0n3m8dQeR2sF5gJ7aL9pX6uI0oE1yK2c\n",
            START_SCRIPT_PATH: UPSTREAM_START,
        }
    )

    def provider() -> list[Instance]:
        return [
            Instance(SOURCE, "Frankfurt", "203.0.113.20", lambda: nullcontext(server.host()), XRAY)
        ]

    instances.register(provider)
    yield server
    instances.unregister(provider)


@pytest.fixture
def sync(django_capture_on_commit_callbacks: OnCommit) -> Callable[[], list[Operation]]:
    """Compare the rules and run the started operations at once, as the worker would."""

    def run() -> list[Operation]:
        with django_capture_on_commit_callbacks(execute=True):
            started = SpeedLimiter().sync_all()
        for operation in started:
            operation.refresh_from_db()
        return started

    return run


@pytest.fixture
def issue(django_capture_on_commit_callbacks: OnCommit) -> Callable[[User], AccessKey]:
    admin = KeyActions(UserFactory(role=Role.ADMIN))

    def run(owner: User) -> AccessKey:
        with django_capture_on_commit_callbacks(execute=True):
            admin.issue(SOURCE, "iPhone", owner)
        return AccessKey.objects.latest("pk")

    return run


def subscriber(speed: int | None = None) -> User:
    limits: UserLimits = UserLimitsFactory(plan=PlanFactory(speed_limit=speed))
    return limits.user


def lost(at: datetime) -> None:
    """The collector got no statistics at `at`."""
    TrafficSource.objects.update_or_create(
        source=SOURCE, defaults={"available": False, "checked_at": at}
    )


def routing(server: FakeXrayServer) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = json.loads(server.files[XRAY_CONFIG_PATH])
    return rules.get("routing", {}).get("rules", []) if isinstance(rules, dict) else []


def test_limit_routes_the_key_and_polices_its_mark(
    server: XrayWithApi, issue: Callable[[User], AccessKey], sync: Callable[[], list[Operation]]
) -> None:
    key = issue(subscriber(speed=10))
    issue(subscriber())
    restarts = server.restarts

    (operation,) = sync()

    assert operation.status == OperationStatus.SUCCEEDED
    assert operation.params == {
        "source": SOURCE,
        "rules": [XraySpeedRule(key.public_key, 10).to_json()],
    }
    assert routing(server) == [
        {"type": "field", "user": [key.public_key], "outboundTag": "lamnezia-1001"}
    ]
    assert server.restarts == restarts + 1
    assert "ct mark 1001 limit rate over 1250 kbytes/second" in server.files[NFT]
    assert f"nft -f {NFT}" in "\n".join(server.commands)
    assert server.files[START_SCRIPT_PATH].splitlines()[1:3] == [XRAY_STOP_LINE, HOOK]


def test_a_new_speed_reloads_the_rules_without_a_restart(
    server: XrayWithApi, issue: Callable[[User], AccessKey], sync: Callable[[], list[Operation]]
) -> None:
    user = subscriber(speed=10)
    issue(user)
    sync()
    restarts = server.restarts

    UserLimits.objects.filter(user=user).update(speed_limit=20)
    (operation,) = sync()

    assert operation.status == OperationStatus.SUCCEEDED
    assert "rate over 2500 kbytes/second" in server.files[NFT]
    assert server.restarts == restarts
    assert server.files[START_SCRIPT_PATH].count(HOOK) == 1


def test_removed_limit_takes_the_routes_out(
    server: XrayWithApi, issue: Callable[[User], AccessKey], sync: Callable[[], list[Operation]]
) -> None:
    user = subscriber(speed=10)
    issue(user)
    sync()

    UserLimits.objects.filter(user=user).update(plan=PlanFactory())
    (operation,) = sync()

    assert operation.params["rules"] == []
    assert routing(server) == []
    assert "lamnezia-" not in server.files[XRAY_CONFIG_PATH]
    assert "limit rate" not in server.files[NFT]


def test_limits_come_back_after_the_app_recreated_the_container(
    server: XrayWithApi, issue: Callable[[User], AccessKey], sync: Callable[[], list[Operation]]
) -> None:
    key = issue(subscriber(speed=10))
    (first,) = sync()
    assert sync() == []

    # The app wrote its own server.json and start.sh: no statistics, routes or nft line.
    server.files[XRAY_CONFIG_PATH] = APP_CONFIG
    server.files[START_SCRIPT_PATH] = UPSTREAM_START
    assert first.finished_at is not None
    lost(first.finished_at + timedelta(minutes=5))
    (again,) = sync()

    assert again.status == OperationStatus.SUCCEEDED
    assert routing(server)[0]["user"] == [key.public_key]
    assert '"stats"' in server.files[XRAY_CONFIG_PATH]
    assert HOOK in server.files[START_SCRIPT_PATH]


def test_statistics_lost_before_the_operation_start_nothing(
    server: XrayWithApi, issue: Callable[[User], AccessKey], sync: Callable[[], list[Operation]]
) -> None:
    issue(subscriber(speed=10))
    lost(timezone.now())
    sync()

    assert sync() == []


def test_operation_fails_while_xray_does_not_answer(
    server: XrayWithApi, issue: Callable[[User], AccessKey], sync: Callable[[], list[Operation]]
) -> None:
    issue(subscriber(speed=10))
    server.api = False

    (operation,) = sync()

    assert operation.status == OperationStatus.FAILED
    lost(timezone.now() + timedelta(minutes=5))
    assert sync() == []
    assert Operation.objects.filter(kind=SPEED_APPLY).count() == 1
