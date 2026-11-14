"""The relay monitor: reading the agent state, keeping it and notifying of what changed."""

import json
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from apps.groups.models import GroupMember, ServerGroup
from apps.groups.tests.conftest import JOINER_HOST, LEADER_HOST, PORT
from apps.notifications.events import Event
from apps.notifications.models import Notification
from apps.notifications.tests.factories import ChannelFactory
from apps.relays.models import Relay
from apps.relays.services.monitor import (
    BAD_STATE,
    STALE,
    STATE_PATH,
    RelayMonitor,
    report_lagging,
)
from apps.relays.tests.fake_relay import FakeRelayServer
from apps.servers.models import Server
from apps.ssh.errors import ErrorCode, SshConnectionError
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db

AT = 1731580000
NOW = datetime.fromtimestamp(AT, UTC)
INTERVAL = timedelta(seconds=60)
EVENTS = [
    Event.RELAY_SWITCHED,
    Event.MEMBER_UNREACHABLE,
    Event.GROUP_UNREACHABLE,
    Event.RELAY_DOWN,
    Event.MEMBER_LAGGING,
]


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture(autouse=True)
def channel() -> None:
    ChannelFactory(events=EVENTS)


@pytest.fixture
def relay(relayed: ServerGroup, relay_fake: FakeRelayServer) -> Relay:
    relay_fake.state = "running"
    assert relayed.relay is not None
    return Relay.objects.create(server=relayed.relay, installed_at=NOW - timedelta(hours=1))


@pytest.fixture
def clock() -> Clock:
    return Clock()


def answering(fake: FakeRelayServer) -> Callable[[Server], AbstractContextManager[RemoteHost]]:
    @contextmanager
    def connect(_server: Server) -> Iterator[RemoteHost]:
        yield fake.host()

    return connect


@contextmanager
def silent(_server: Server) -> Iterator[RemoteHost]:
    raise SshConnectionError(ErrorCode.CONNECTION_TIMEOUT)
    yield  # pragma: no cover


def backend(host: str, *, alive: bool = True, failures: int = 0) -> dict[str, Any]:
    return {"host": host, "port": PORT, "alive": alive, "failures": failures, "checked_at": AT}


def write_state(  # noqa: PLR0913 - one argument per field of the state file
    fake: FakeRelayServer,
    *,
    at: int = AT,
    active: str | None = LEADER_HOST,
    switched_at: int | None = None,
    backends: list[dict[str, Any]] | None = None,
    sent: int = 0,
) -> None:
    group = {
        "port": PORT,
        "active": {"host": active, "port": PORT} if active else None,
        "switched_at": switched_at,
        "backends": backends or [backend(LEADER_HOST), backend(JOINER_HOST)],
        "bytes_from_clients": sent,
        "bytes_to_clients": sent * 2,
    }
    state = {"version": 1, "updated_at": at, "groups": [group]}
    fake.files[STATE_PATH] = json.dumps(state).encode()


def monitor(fake: FakeRelayServer, clock: Clock) -> RelayMonitor:
    return RelayMonitor(INTERVAL, connect=answering(fake), clock=clock)


def told(event: Event) -> list[dict[str, Any]]:
    return [notice.data for notice in Notification.objects.filter(event=event).order_by("pk")]


def test_a_reading_is_kept_with_the_relay(
    relay: Relay, relay_fake: FakeRelayServer, clock: Clock
) -> None:
    write_state(relay_fake)

    monitor(relay_fake, clock).poll(relay)

    relay.refresh_from_db()
    leader = GroupMember.objects.get(leading=True)
    health = relay.group_health(PORT)
    assert health is not None
    assert health.active is not None and health.active.id == leader.pk
    assert relay.read_at == NOW
    assert relay.read_error == ""
    assert not Notification.objects.exists()


def test_a_switch_is_kept_and_told_once(
    relay: Relay, relay_fake: FakeRelayServer, clock: Clock
) -> None:
    watch = monitor(relay_fake, clock)
    write_state(relay_fake)
    watch.poll(relay)

    write_state(
        relay_fake,
        at=AT + 60,
        active=JOINER_HOST,
        switched_at=AT + 30,
        backends=[backend(LEADER_HOST, alive=False, failures=3), backend(JOINER_HOST)],
    )
    clock.now = NOW + timedelta(seconds=60)
    watch.poll(relay)
    watch.poll(relay)

    relay.refresh_from_db()
    joiner = GroupMember.objects.get(leading=False)
    health = relay.group_health(PORT)
    assert health is not None and health.active is not None
    assert health.active.id == joiner.pk
    assert health.switched_at == AT + 30
    leader_name = GroupMember.objects.get(leading=True).instance.server.name
    assert told(Event.RELAY_SWITCHED) == [
        {
            "group": relay.server.relayed_groups.get().name,
            "relay": relay.server.name,
            "from": leader_name,
            "to": joiner.instance.server.name,
        }
    ]
    assert [data["server"] for data in told(Event.MEMBER_UNREACHABLE)] == [leader_name]


def test_a_group_with_no_member_left_is_told(
    relay: Relay, relay_fake: FakeRelayServer, clock: Clock
) -> None:
    down = [
        backend(LEADER_HOST, alive=False, failures=3),
        backend(JOINER_HOST, alive=False, failures=4),
    ]
    write_state(relay_fake, active=None, backends=down)

    monitor(relay_fake, clock).poll(relay)

    assert len(told(Event.MEMBER_UNREACHABLE)) == 2
    assert told(Event.GROUP_UNREACHABLE) == [
        {"group": relay.server.relayed_groups.get().name, "relay": relay.server.name}
    ]


def test_the_load_comes_from_two_readings(
    relay: Relay, relay_fake: FakeRelayServer, clock: Clock
) -> None:
    watch = monitor(relay_fake, clock)
    write_state(relay_fake, sent=1000)
    watch.poll(relay)
    write_state(relay_fake, at=AT + 10, sent=3000)
    clock.now = NOW + timedelta(seconds=10)
    watch.poll(relay)

    relay.refresh_from_db()
    assert relay.rate_from_clients == 200.0
    assert relay.rate_to_clients == 400.0


@pytest.mark.parametrize(
    ("content", "code"),
    [(b"not json", BAD_STATE), (None, STALE)],
)
def test_a_state_not_to_trust_is_an_error(
    relay: Relay, relay_fake: FakeRelayServer, clock: Clock, content: bytes | None, code: str
) -> None:
    write_state(relay_fake, at=AT - 3 * 60)
    if content is not None:
        relay_fake.files[STATE_PATH] = content

    monitor(relay_fake, clock).poll(relay)

    relay.refresh_from_db()
    assert relay.read_error == code
    assert relay.read_at is None


def test_a_missing_state_is_an_error(
    relay: Relay, relay_fake: FakeRelayServer, clock: Clock
) -> None:
    monitor(relay_fake, clock).poll(relay)

    relay.refresh_from_db()
    assert relay.read_error


def test_a_relay_silent_for_two_intervals_is_told_once(
    relay: Relay, relay_fake: FakeRelayServer, clock: Clock
) -> None:
    write_state(relay_fake)
    monitor(relay_fake, clock).poll(relay)
    watch = RelayMonitor(INTERVAL, connect=silent, clock=clock)

    clock.now = NOW + INTERVAL
    watch.poll(relay)
    assert not told(Event.RELAY_DOWN)

    clock.now = NOW + 3 * INTERVAL
    watch.poll(relay)
    watch.poll(relay)

    relay.refresh_from_db()
    assert relay.read_error == ErrorCode.CONNECTION_TIMEOUT
    assert told(Event.RELAY_DOWN) == [
        {"relay": relay.server.name, "host": relay.server.host, "last_seen": NOW.isoformat()}
    ]


def test_a_member_lagging_too_long_is_told_once(relayed: ServerGroup) -> None:
    joiner = GroupMember.objects.get(leading=False)
    joiner.lagging_since = NOW - timedelta(hours=2)
    joiner.save(update_fields=["lagging_since"])
    leader = GroupMember.objects.get(leading=True)
    leader.lagging_since = NOW - timedelta(minutes=10)
    leader.save(update_fields=["lagging_since"])

    report_lagging(timedelta(hours=1), now=NOW)
    report_lagging(timedelta(hours=1), now=NOW)

    assert told(Event.MEMBER_LAGGING) == [
        {
            "group": relayed.name,
            "server": joiner.instance.server.name,
            "since": joiner.lagging_since.isoformat(),
        }
    ]
