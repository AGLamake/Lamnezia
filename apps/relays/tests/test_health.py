"""The health of the relay groups: matching the agent state to members and what changed."""

import json
from ipaddress import IPv4Address
from typing import Any

from amnezia_core.relay import Backend, RelayState
from apps.relays.services.health import (
    AllDown,
    GroupHealth,
    Member,
    MemberDown,
    RelayHealth,
    Switched,
)

PORT = 55424
FIRST = Backend(IPv4Address("203.0.113.10"), PORT)
SECOND = Backend(IPv4Address("203.0.113.20"), PORT)
LEADER, JOINER = Member(1, "Leader"), Member(2, "Joiner")
MEMBERS = {PORT: {FIRST: LEADER, SECOND: JOINER}}
AT = 1731580000


def backend(address: Backend, *, alive: bool = True, failures: int = 0) -> dict[str, Any]:
    return {
        "host": str(address.host),
        "port": address.port,
        "alive": alive,
        "failures": failures,
        "checked_at": AT,
    }


def reading(  # noqa: PLR0913 - one argument per field of the state file
    *,
    at: int = AT,
    active: Backend | None = FIRST,
    switched_at: int | None = None,
    backends: list[dict[str, Any]] | None = None,
    sent: int = 0,
    received: int = 0,
) -> RelayHealth:
    group = {
        "port": PORT,
        "active": active.to_dict() if active else None,
        "switched_at": switched_at,
        "backends": backends if backends is not None else [backend(FIRST), backend(SECOND)],
        "bytes_from_clients": sent,
        "bytes_to_clients": received,
    }
    text = json.dumps({"version": 1, "updated_at": at, "groups": [group]})
    return RelayHealth.of(RelayState.parse(text), MEMBERS)


def test_members_are_matched_by_address() -> None:
    down = [backend(FIRST), backend(SECOND, alive=False, failures=3)]
    group = reading(backends=down).groups[PORT]

    assert group.active == LEADER
    assert group.alive == (LEADER,)
    assert group.down == (JOINER,)
    assert group.reachable(LEADER.id) is True
    assert group.reachable(JOINER.id) is False
    assert group.reachable(99) is None


def test_a_member_with_fewer_failures_than_a_switch_needs_is_still_alive() -> None:
    group = reading(backends=[backend(FIRST, alive=False, failures=2)]).groups[PORT]

    assert group.alive == (LEADER,)
    assert group.down == ()


def test_an_address_of_no_member_is_left_out() -> None:
    stranger = Backend(IPv4Address("198.51.100.1"), PORT)

    group = reading(active=stranger, backends=[backend(stranger)]).groups[PORT]

    assert group.active is None
    assert group.alive == ()


def test_the_health_survives_storing() -> None:
    health = reading(switched_at=AT - 30, sent=10, received=20)

    assert RelayHealth.load(json.loads(json.dumps(health.to_dict()))) == health


def test_stored_health_of_an_unknown_form_is_none() -> None:
    assert RelayHealth.load({}) is None
    assert RelayHealth.load({"updated_at": AT, "groups": [{"active": None}]}) is None


def test_a_new_active_member_is_a_switch() -> None:
    before = reading()
    after = reading(at=AT + 60, active=SECOND, switched_at=AT + 30)

    assert list(after.changes(before)) == [Switched(PORT, LEADER, JOINER, AT + 30)]


def test_the_first_reading_is_no_switch() -> None:
    assert list(reading(active=SECOND, switched_at=AT).changes(None)) == []


def test_a_member_that_goes_down_is_told_once() -> None:
    down = [backend(FIRST), backend(SECOND, alive=False, failures=3)]
    first = reading(backends=down)

    assert list(first.changes(reading())) == [MemberDown(PORT, JOINER)]
    assert list(reading(at=AT + 60, backends=down).changes(first)) == []


def test_all_members_down_is_told_once() -> None:
    down = [backend(FIRST, alive=False, failures=3), backend(SECOND, alive=False, failures=5)]
    first = reading(active=None, backends=down)

    assert list(first.changes(reading())) == [
        MemberDown(PORT, LEADER),
        MemberDown(PORT, JOINER),
        AllDown(PORT),
    ]
    assert first.groups[PORT].all_down
    assert list(reading(at=AT + 60, active=None, backends=down).changes(first)) == []


def test_rates_come_from_the_counters_of_two_readings() -> None:
    before = reading(sent=1000, received=4000)
    after = reading(at=AT + 10, sent=3000, received=5000)

    assert after.rates(before) == (200.0, 100.0)


def test_rates_are_unknown_without_a_previous_reading_or_after_a_restart() -> None:
    before = reading(sent=1000, received=4000)

    assert before.rates(None) is None
    assert before.rates(before) is None
    assert reading(at=AT + 10, sent=10, received=10).rates(before) is None


def test_a_group_of_no_backends_has_nothing_down() -> None:
    group = GroupHealth(PORT, None, None, (), (), 0, 0)

    assert not group.all_down
