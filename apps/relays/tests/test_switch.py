"""A planned switch of a relayed group: the request to the agent, its answer and the event."""

import functools
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from amnezia_core.relay import DEFAULT_FAILURES, SwitchOutcome
from apps.accounts.models import User
from apps.groups.models import GroupMember, ServerGroup
from apps.groups.services.membership import start_leave
from apps.groups.tests.conftest import JOINER_HOST, LEADER_HOST, PORT, Fakes, succeeded
from apps.notifications.events import Event
from apps.notifications.models import Notification
from apps.notifications.tests.factories import ChannelFactory
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.launcher import launch
from apps.protocols.tests.awg_case import Start
from apps.relays.errors import RelayError, RelayErrorCode
from apps.relays.models import Relay
from apps.relays.services import switch
from apps.relays.services.monitor import STATE_PATH, RelayMonitor
from apps.relays.services.switch import REQUEST_PATH, SWITCH_KIND, start_switch
from apps.relays.tests.conftest import RELAY_HOST
from apps.relays.tests.fake_relay import FakeRelayServer
from apps.servers.models import Server
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db

INTERVAL = timedelta(seconds=60)


class FakeAgent:
    """Stands in for the agent while the panel waits: each pause it takes a request and answers.

    `outcome` None leaves the request unanswered, as an agent of an older release does.
    """

    def __init__(self, fake: FakeRelayServer, outcome: SwitchOutcome | None) -> None:
        self.fake = fake
        self.outcome = outcome
        self.now = 0.0
        self.requests: list[dict[str, Any]] = []

    def pause(self, seconds: float) -> None:
        self.now += seconds
        text = self.fake.files.get(REQUEST_PATH)
        if text is None or self.outcome is None:
            return
        request = json.loads(text)
        self.requests.append(request)
        del self.fake.files[REQUEST_PATH]
        moved = self.outcome == SwitchOutcome.SWITCHED
        active = request["backend"]["host"] if moved else LEADER_HOST
        write_state(self.fake, active, switched=moved)
        state = json.loads(self.fake.files[STATE_PATH])
        state["switch"] = {"id": request["id"], "outcome": self.outcome}
        self.fake.files[STATE_PATH] = json.dumps(state).encode()

    def clock(self) -> float:
        return self.now


def write_state(
    fake: FakeRelayServer, active: str, *, switched: bool = False, down: tuple[str, ...] = ()
) -> None:
    """The agent state with the group on `active`; the members of `down` fail their probes."""
    now = int(timezone.now().timestamp())
    backends = [
        {
            "host": host,
            "port": PORT,
            "alive": host not in down,
            "failures": DEFAULT_FAILURES if host in down else 0,
            "checked_at": now,
        }
        for host in (LEADER_HOST, JOINER_HOST)
    ]
    group = {
        "port": PORT,
        "active": {"host": active, "port": PORT},
        "switched_at": now if switched else None,
        "backends": backends,
        "bytes_from_clients": 0,
        "bytes_to_clients": 0,
    }
    state = {"version": 1, "updated_at": now, "groups": [group]}
    fake.files[STATE_PATH] = json.dumps(state).encode()


@pytest.fixture
def relay(
    relayed: ServerGroup,
    relay_fake: FakeRelayServer,
    servers: Fakes,
    monkeypatch: pytest.MonkeyPatch,
) -> Relay:
    """An installed relay that sends the clients to the leading server, as the panel last read."""

    @contextmanager
    def connect(server: Server) -> Iterator[RemoteHost]:
        yield relay_fake.host() if server.host == RELAY_HOST else servers[server.host].host()

    monkeypatch.setattr("apps.relays.services.switch.connect", connect)
    ChannelFactory(events=[Event.RELAY_SWITCHED])
    relay_fake.state = "running"
    assert relayed.relay is not None
    relay = Relay.objects.create(server=relayed.relay, installed_at=timezone.now())
    write_state(relay_fake, LEADER_HOST)
    RelayMonitor(INTERVAL, connect=connect).poll(relay)
    return relay


@pytest.fixture
def joiner_member(relay: Relay) -> GroupMember:
    return GroupMember.objects.get(leading=False)


Switch = Callable[[SwitchOutcome | None], tuple[Operation, FakeAgent]]


@pytest.fixture
def switch_to_joiner(
    joiner_member: GroupMember,
    relay_fake: FakeRelayServer,
    admin: User,
    start: Start,
    monkeypatch: pytest.MonkeyPatch,
) -> Switch:
    """Run the switch to the joined member with an agent that answers `outcome`."""

    def run(outcome: SwitchOutcome | None) -> tuple[Operation, FakeAgent]:
        agent = FakeAgent(relay_fake, outcome)
        patched = functools.partial(switch.RelaySwitch, pause=agent.pause, clock=agent.clock)
        monkeypatch.setattr("apps.relays.operations.RelaySwitch", patched)
        return start(lambda: start_switch(joiner_member, user=admin)), agent

    return run


def test_the_clients_go_to_the_chosen_member_and_it_is_told(switch_to_joiner: Switch) -> None:
    operation, agent = switch_to_joiner(SwitchOutcome.SWITCHED)

    succeeded(operation)
    assert [request["backend"] for request in agent.requests] == [
        {"host": JOINER_HOST, "port": PORT}
    ]
    relay = Relay.objects.get()
    health = relay.group_health(PORT)
    joiner = GroupMember.objects.get(leading=False)
    assert health is not None and health.active is not None
    assert health.active.id == joiner.pk
    switched = Notification.objects.get(event=Event.RELAY_SWITCHED)
    assert switched.data["to"] == joiner.instance.server.name


def test_an_unreachable_member_is_refused_and_nothing_changes(switch_to_joiner: Switch) -> None:
    operation, _agent = switch_to_joiner(SwitchOutcome.UNREACHABLE)

    assert operation.status == OperationStatus.FAILED
    assert operation.error_code == RelayErrorCode.SWITCH_UNREACHABLE
    assert not Notification.objects.filter(event=Event.RELAY_SWITCHED).exists()


def test_a_member_the_agent_does_not_know_asks_for_an_update(switch_to_joiner: Switch) -> None:
    operation, _agent = switch_to_joiner(SwitchOutcome.UNKNOWN_BACKEND)

    assert operation.error_code == RelayErrorCode.SWITCH_OUTDATED


def test_the_active_member_needs_nothing(switch_to_joiner: Switch) -> None:
    operation, _agent = switch_to_joiner(SwitchOutcome.ALREADY_ACTIVE)

    succeeded(operation)
    assert not Notification.objects.filter(event=Event.RELAY_SWITCHED).exists()


def test_an_agent_that_does_not_answer_gets_its_request_back(
    switch_to_joiner: Switch, relay_fake: FakeRelayServer
) -> None:
    operation, agent = switch_to_joiner(None)

    assert operation.error_code == RelayErrorCode.SWITCH_NO_ANSWER
    assert REQUEST_PATH not in relay_fake.files
    assert agent.now >= switch.ANSWER_TIMEOUT


def test_an_old_answer_is_not_taken_for_this_request(
    switch_to_joiner: Switch, relay_fake: FakeRelayServer
) -> None:
    state = json.loads(relay_fake.files[STATE_PATH])
    state["switch"] = {"id": "earlier", "outcome": "switched"}
    relay_fake.files[STATE_PATH] = json.dumps(state).encode()

    operation, _agent = switch_to_joiner(None)

    assert operation.error_code == RelayErrorCode.SWITCH_NO_ANSWER


def test_a_lagging_member_is_refused(joiner_member: GroupMember, admin: User) -> None:
    joiner_member.lagging_since = timezone.now()
    joiner_member.save(update_fields=["lagging_since"])

    with pytest.raises(RelayError) as error:
        start_switch(joiner_member, user=admin)

    assert error.value.code == RelayErrorCode.SWITCH_NOT_BACKEND


def test_a_group_without_an_installed_relay_is_refused(
    joiner_member: GroupMember, admin: User
) -> None:
    Relay.objects.update(installed_at=None)

    with pytest.raises(RelayError) as error:
        start_switch(joiner_member, user=admin)

    assert error.value.code == RelayErrorCode.NOT_INSTALLED


def test_one_switch_of_a_group_at_a_time(joiner_member: GroupMember, admin: User) -> None:
    Operation.objects.create(
        kind=SWITCH_KIND,
        status=OperationStatus.RUNNING,
        params={"group_id": joiner_member.group_id},
    )

    with pytest.raises(RelayError) as error:
        start_switch(joiner_member, user=admin)

    assert error.value.code == RelayErrorCode.SWITCH_RUNNING


def test_a_member_gone_before_the_run_leaves_nothing_to_do(
    joiner_member: GroupMember, admin: User, start: Start, monkeypatch: pytest.MonkeyPatch
) -> None:
    def gone(*args: Any, **kwargs: Any) -> Operation:
        operation = launch(*args, **kwargs)
        GroupMember.objects.filter(pk=joiner_member.pk).delete()
        return operation

    monkeypatch.setattr("apps.relays.services.switch.launch", gone)

    succeeded(start(lambda: start_switch(joiner_member, user=admin)))


Leave = Callable[[SwitchOutcome | None], tuple[Operation, FakeAgent]]


@pytest.fixture
def leave_joiner(
    joiner_member: GroupMember,
    relay_fake: FakeRelayServer,
    admin: User,
    start: Start,
    monkeypatch: pytest.MonkeyPatch,
) -> Leave:
    """Run the leave of the joined member with an agent that answers a switch `outcome`."""

    def run(outcome: SwitchOutcome | None) -> tuple[Operation, FakeAgent]:
        agent = FakeAgent(relay_fake, outcome)
        patched = functools.partial(switch.LeaveSwitch, pause=agent.pause, clock=agent.clock)
        monkeypatch.setattr("apps.groups.operations.LeaveSwitch", patched)
        return start(lambda: start_leave(joiner_member, user=admin)), agent

    return run


def joiner_active(relay: Relay, relay_fake: FakeRelayServer, *, down: tuple[str, ...] = ()) -> None:
    write_state(relay_fake, JOINER_HOST, down=down)
    RelayMonitor(INTERVAL, connect=lambda server: _relay_host(relay_fake)).poll(relay)


@contextmanager
def _relay_host(relay_fake: FakeRelayServer) -> Iterator[RemoteHost]:
    yield relay_fake.host()


def step_logs(operation: Operation) -> str:
    return "\n".join(f"{step.title}: {step.log}" for step in operation.steps.order_by("position"))


def test_leaving_the_active_member_switches_the_relay_away_first(
    relay: Relay, relay_fake: FakeRelayServer, leave_joiner: Leave
) -> None:
    joiner_active(relay, relay_fake)

    operation, agent = leave_joiner(SwitchOutcome.SWITCHED)

    succeeded(operation)
    assert [request["backend"]["host"] for request in agent.requests] == [LEADER_HOST]
    titles = list(operation.steps.order_by("position").values_list("title", flat=True))
    assert titles.index("Ask the relay to switch") < titles.index("Write a clean AmneziaWG")
    assert not GroupMember.objects.filter(leading=False).exists()


def test_leaving_a_member_the_relay_does_not_use_needs_no_switch(
    relay: Relay, leave_joiner: Leave
) -> None:
    operation, agent = leave_joiner(SwitchOutcome.SWITCHED)

    succeeded(operation)
    assert agent.requests == []
    assert "no switch needed" in step_logs(operation)


def test_without_another_member_to_take_the_clients_the_leave_warns(
    relay: Relay, relay_fake: FakeRelayServer, leave_joiner: Leave
) -> None:
    joiner_active(relay, relay_fake, down=(LEADER_HOST,))

    operation, agent = leave_joiner(SwitchOutcome.SWITCHED)

    succeeded(operation)
    assert agent.requests == []
    assert "Warning:" in step_logs(operation)
    assert not GroupMember.objects.filter(leading=False).exists()


def test_a_refused_switch_leaves_with_a_warning(
    relay: Relay, relay_fake: FakeRelayServer, leave_joiner: Leave
) -> None:
    joiner_active(relay, relay_fake)

    operation, agent = leave_joiner(SwitchOutcome.UNREACHABLE)

    succeeded(operation)
    assert len(agent.requests) == 1
    logs = step_logs(operation)
    assert "Leave without a planned switch" in logs and "Warning:" in logs
    assert not GroupMember.objects.filter(leading=False).exists()
