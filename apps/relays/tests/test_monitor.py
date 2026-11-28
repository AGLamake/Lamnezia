"""The relay monitor: reading the agent state, keeping it and notifying of what changed."""

import json
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.utils import translation

from apps.groups.models import GroupMember, LagReason, ServerGroup
from apps.groups.tests.conftest import JOINER_HOST, LEADER_HOST, PORT
from apps.notifications.events import EVENTS as EVENT_TYPES
from apps.notifications.events import Event
from apps.notifications.models import Notification, Route, Trouble
from apps.notifications.tests.factories import ChannelFactory
from apps.relays.models import Relay, SplitSample
from apps.relays.services.health import STEADY_SECONDS, SplitTraffic
from apps.relays.services.monitor import (
    BAD_STATE,
    STALE,
    STATE_PATH,
    RelayMonitor,
    report_caught_up,
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
    Event.MEMBER_REACHABLE,
    Event.GROUP_REACHABLE,
    Event.RELAY_UP,
    Event.MEMBER_CAUGHT_UP,
    Event.SPLIT_EMERGENCY,
    Event.SPLIT_RESTORED,
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
    split: dict[str, Any] | None = None,
) -> None:
    group = {
        "port": PORT,
        "active": {"host": active, "port": PORT} if active else None,
        "switched_at": switched_at,
        "backends": backends or [backend(LEADER_HOST), backend(JOINER_HOST)],
        "bytes_from_clients": sent,
        "bytes_to_clients": sent * 2,
        **(split or {}),
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


def split(mode: str, changed: int) -> dict[str, Any]:
    return {
        "routing": "split",
        "mode": mode,
        "mode_changed_at": changed,
        "uplink": {"endpoint": None, "alive": mode == "split", "checked_at": AT},
    }


def test_the_agent_moving_a_split_group_to_forward_is_told_once(
    relay: Relay, relay_fake: FakeRelayServer, clock: Clock
) -> None:
    watch = monitor(relay_fake, clock)
    write_state(relay_fake, split=split("split", AT - 600))
    watch.poll(relay)

    write_state(relay_fake, at=AT + 60, split=split("forward", AT + 30))
    clock.now = NOW + timedelta(seconds=60)
    watch.poll(relay)
    watch.poll(relay)

    leader = GroupMember.objects.get(leading=True).instance.server.name
    group = relay.server.relayed_groups.get()
    assert told(Event.SPLIT_EMERGENCY) == [
        {"group": group.name, "relay": relay.server.name, "server": leader}
    ]
    relay.refresh_from_db()
    health = relay.group_health(PORT)
    assert health is not None and health.emergency


def test_the_agent_moving_a_group_back_to_split_is_told(
    relay: Relay, relay_fake: FakeRelayServer, clock: Clock
) -> None:
    watch = monitor(relay_fake, clock)
    write_state(relay_fake, split=split("forward", AT - 600))
    watch.poll(relay)

    write_state(relay_fake, at=AT + 60, split=split("split", AT + 50))
    clock.now = NOW + timedelta(seconds=60)
    watch.poll(relay)

    group = relay.server.relayed_groups.get()
    assert told(Event.SPLIT_RESTORED) == [{"group": group.name, "relay": relay.server.name}]
    assert not told(Event.SPLIT_EMERGENCY)


def test_the_mode_notifications_read_in_both_languages() -> None:
    data = {"group": "Europe", "relay": "Moscow", "server": "Helsinki"}

    title, text = EVENT_TYPES[Event.SPLIT_EMERGENCY].render(data)

    assert title == "Europe: emergency forwarding"
    assert "Helsinki" in text
    with translation.override("ru"):
        title, _text = EVENT_TYPES[Event.SPLIT_RESTORED].render(data)
    assert title == "Europe: снова раздельный режим"


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
    # The first reading has no load yet, so only the second one is kept for the chart.
    assert list(relay.samples.values_list("at", "rate_from_clients", "rate_to_clients")) == [
        (NOW + timedelta(seconds=10), 200.0, 400.0)
    ]


def direct(outgoing: int, incoming: int) -> dict[str, Any]:
    return {
        **split("split", AT - 600),
        "bytes_direct_from_clients": outgoing,
        "bytes_direct_to_clients": incoming,
    }


def test_the_split_traffic_comes_from_two_readings(
    relay: Relay, relay_fake: FakeRelayServer, clock: Clock
) -> None:
    watch = monitor(relay_fake, clock)
    write_state(relay_fake, sent=1000, split=direct(100, 200))
    watch.poll(relay)
    # 2000 + 4000 bytes in 10 s, of them 500 + 1500 straight out of the relay.
    write_state(relay_fake, at=AT + 10, sent=3000, split=direct(600, 1700))
    clock.now = NOW + timedelta(seconds=10)
    watch.poll(relay)

    relay.refresh_from_db()
    assert relay.split_traffic == SplitTraffic(abroad=400.0, direct=200.0)
    group = relay.server.relayed_groups.get()
    assert list(group.split_samples.values_list("at", "rate_abroad", "rate_direct")) == [
        (NOW + timedelta(seconds=10), 400.0, 200.0)
    ]


def test_an_agent_restart_gives_no_split_traffic(
    relay: Relay, relay_fake: FakeRelayServer, clock: Clock
) -> None:
    watch = monitor(relay_fake, clock)
    write_state(relay_fake, sent=1000, split=direct(500, 500))
    watch.poll(relay)
    write_state(relay_fake, at=AT + 10, sent=3000, split=direct(10, 10))
    clock.now = NOW + timedelta(seconds=10)
    watch.poll(relay)

    relay.refresh_from_db()
    assert relay.split_traffic is None
    assert not SplitSample.objects.exists()


def test_an_older_agent_gives_no_split_traffic(
    relay: Relay, relay_fake: FakeRelayServer, clock: Clock
) -> None:
    watch = monitor(relay_fake, clock)
    write_state(relay_fake, sent=1000, split=split("split", AT - 600))
    watch.poll(relay)
    write_state(relay_fake, at=AT + 10, sent=3000, split=split("split", AT - 600))
    clock.now = NOW + timedelta(seconds=10)
    watch.poll(relay)

    relay.refresh_from_db()
    assert relay.rate_from_clients == 200.0
    assert relay.split_traffic is None


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
    joiner.lagging_reason = LagReason.UNREACHABLE
    joiner.save(update_fields=["lagging_since", "lagging_reason"])
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
            "reason": "unreachable",
        }
    ]
    _, text = EVENT_TYPES[Event.MEMBER_LAGGING].render(told(Event.MEMBER_LAGGING)[0])
    assert "was unreachable during a change" in text


def test_a_member_lagging_since_an_older_release_is_told_without_a_reason(
    relayed: ServerGroup,
) -> None:
    joiner = GroupMember.objects.get(leading=False)
    joiner.lagging_since = NOW - timedelta(hours=2)
    joiner.save(update_fields=["lagging_since"])

    report_lagging(timedelta(hours=1), now=NOW)

    (data,) = told(Event.MEMBER_LAGGING)
    assert "reason" not in data
    _, text = EVENT_TYPES[Event.MEMBER_LAGGING].render(data)
    assert "not lagging" not in text


def test_a_member_and_a_group_reachable_again_are_told_once(
    relay: Relay, relay_fake: FakeRelayServer, clock: Clock
) -> None:
    watch = monitor(relay_fake, clock)
    down = [
        backend(LEADER_HOST, alive=False, failures=3),
        backend(JOINER_HOST, alive=False, failures=4),
    ]
    write_state(relay_fake, active=None, backends=down)
    watch.poll(relay)

    group = relay.server.relayed_groups.get().name
    for seconds in range(60, STEADY_SECONDS + 120, 60):
        write_state(relay_fake, at=AT + seconds, backends=[backend(LEADER_HOST), down[1]])
        clock.now = NOW + timedelta(seconds=seconds)
        watch.poll(relay)
        if seconds == 60:
            assert told(Event.GROUP_REACHABLE) == [{"group": group, "relay": relay.server.name}]
            assert told(Event.MEMBER_REACHABLE) == []

    leader = GroupMember.objects.get(leading=True).instance.server.name
    assert told(Event.MEMBER_REACHABLE) == [
        {"group": group, "relay": relay.server.name, "server": leader}
    ]
    assert len(told(Event.GROUP_REACHABLE)) == 1


def test_a_relay_answering_again_is_told_once_after_its_outage(
    relay: Relay, relay_fake: FakeRelayServer, clock: Clock
) -> None:
    watch = monitor(relay_fake, clock)
    write_state(relay_fake)
    watch.poll(relay)
    clock.now = NOW + 3 * INTERVAL
    RelayMonitor(INTERVAL, connect=silent, clock=clock).poll(relay)

    write_state(relay_fake, at=AT + 4 * 60)
    clock.now = NOW + 4 * INTERVAL
    watch.poll(relay)
    write_state(relay_fake, at=AT + 5 * 60)
    clock.now = NOW + 5 * INTERVAL
    watch.poll(relay)

    assert told(Event.RELAY_UP) == [
        {"relay": relay.server.name, "host": relay.server.host, "last_seen": NOW.isoformat()}
    ]


def test_a_slow_reading_started_before_the_outage_ends_it(
    relay: Relay, relay_fake: FakeRelayServer, clock: Clock
) -> None:
    watch = monitor(relay_fake, clock)
    write_state(relay_fake)
    watch.poll(relay)
    slow = Relay.objects.select_related("server").get(pk=relay.pk)
    clock.now = NOW + INTERVAL
    watch.poll(relay)
    clock.now = NOW + 4 * INTERVAL
    RelayMonitor(INTERVAL, connect=silent, clock=clock).poll(relay)

    write_state(relay_fake, at=AT + 5 * 60)
    clock.now = NOW + 5 * INTERVAL
    watch.poll(slow)
    relay.refresh_from_db()
    write_state(relay_fake, at=AT + 6 * 60)
    clock.now = NOW + 6 * INTERVAL
    watch.poll(relay)

    assert told(Event.RELAY_UP) == [
        {
            "relay": relay.server.name,
            "host": relay.server.host,
            "last_seen": (NOW + INTERVAL).isoformat(),
        }
    ]
    assert not Trouble.objects.filter(key__startswith="relay_down:").exists()


def test_a_relay_answering_again_is_told_with_nobody_told_it_was_down(
    relay: Relay, relay_fake: FakeRelayServer, clock: Clock
) -> None:
    Route.objects.exclude(event=Event.RELAY_UP).delete()
    watch = monitor(relay_fake, clock)
    write_state(relay_fake)
    watch.poll(relay)
    clock.now = NOW + 3 * INTERVAL
    RelayMonitor(INTERVAL, connect=silent, clock=clock).poll(relay)

    write_state(relay_fake, at=AT + 4 * 60)
    clock.now = NOW + 4 * INTERVAL
    watch.poll(relay)

    assert not told(Event.RELAY_DOWN)
    assert [notice["relay"] for notice in told(Event.RELAY_UP)] == [relay.server.name]


def test_a_relay_not_told_down_is_not_told_up(
    relay: Relay, relay_fake: FakeRelayServer, clock: Clock
) -> None:
    watch = monitor(relay_fake, clock)
    write_state(relay_fake)
    watch.poll(relay)
    clock.now = NOW + INTERVAL
    RelayMonitor(INTERVAL, connect=silent, clock=clock).poll(relay)

    write_state(relay_fake, at=AT + 2 * 60)
    clock.now = NOW + 2 * INTERVAL
    watch.poll(relay)

    assert not told(Event.RELAY_DOWN)
    assert not told(Event.RELAY_UP)


def test_a_member_caught_up_is_told_once_after_its_lag(relayed: ServerGroup) -> None:
    joiner = GroupMember.objects.get(leading=False)
    since = NOW - timedelta(hours=2)
    joiner.lagging_since = since
    joiner.save(update_fields=["lagging_since"])
    report_lagging(timedelta(hours=1), now=NOW)
    report_caught_up()
    assert not told(Event.MEMBER_CAUGHT_UP)

    joiner.lagging_since = None
    joiner.save(update_fields=["lagging_since"])
    report_caught_up()
    report_caught_up()

    assert told(Event.MEMBER_CAUGHT_UP) == [
        {"group": relayed.name, "server": joiner.instance.server.name, "since": since.isoformat()}
    ]


def test_a_member_caught_up_is_told_with_nobody_told_it_lagged(relayed: ServerGroup) -> None:
    Route.objects.exclude(event=Event.MEMBER_CAUGHT_UP).delete()
    joiner = GroupMember.objects.get(leading=False)
    joiner.lagging_since = NOW - timedelta(hours=2)
    joiner.save(update_fields=["lagging_since"])
    report_lagging(timedelta(hours=1), now=NOW)

    joiner.lagging_since = None
    joiner.save(update_fields=["lagging_since"])
    report_caught_up()

    assert not told(Event.MEMBER_LAGGING)
    assert [notice["server"] for notice in told(Event.MEMBER_CAUGHT_UP)] == [
        joiner.instance.server.name
    ]


def test_a_member_never_told_lagging_is_not_told_caught_up(relayed: ServerGroup) -> None:
    leader = GroupMember.objects.get(leading=True)
    leader.lagging_since = NOW - timedelta(minutes=10)
    leader.save(update_fields=["lagging_since"])
    report_lagging(timedelta(hours=1), now=NOW)

    leader.lagging_since = None
    leader.save(update_fields=["lagging_since"])
    report_caught_up()

    assert not told(Event.MEMBER_CAUGHT_UP)
