"""The health of the relay groups: matching the agent state to members and what changed."""

import json
from ipaddress import IPv4Address
from typing import Any

from amnezia_core.relay import Backend, RelayState, ResolverState, ResolverStatus, Routing
from apps.relays.services.health import (
    STEADY_SECONDS,
    AllDown,
    GroupHealth,
    GroupUp,
    Member,
    MemberDown,
    MemberUp,
    ModeChanged,
    Outage,
    RelayHealth,
    SplitTraffic,
    Switched,
    Uplink,
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
    unanswered: bool | None = None,
    previous: RelayHealth | None = None,
    split: dict[str, Any] | None = None,
) -> RelayHealth:
    group = {
        "port": PORT,
        "active": active.to_dict() if active else None,
        "switched_at": switched_at,
        "backends": backends if backends is not None else [backend(FIRST), backend(SECOND)],
        "bytes_from_clients": sent,
        "bytes_to_clients": received,
    }
    if unanswered is not None:
        group["unanswered"] = unanswered
    group.update(split or {})
    text = json.dumps({"version": 1, "updated_at": at, "groups": [group]})
    return RelayHealth.of(RelayState.parse(text), MEMBERS, previous)


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


def test_clients_without_an_answer_are_kept() -> None:
    health = reading(unanswered=True)

    assert health.groups[PORT].unanswered
    assert RelayHealth.load(json.loads(json.dumps(health.to_dict()))) == health


def test_an_older_agent_says_nothing_about_the_clients() -> None:
    assert not reading().groups[PORT].unanswered


def test_stored_health_of_an_unknown_form_is_none() -> None:
    assert RelayHealth.load({}) is None
    assert RelayHealth.load({"updated_at": AT, "groups": [{"active": None}]}) is None


def split(mode: str = "split", *, alive: bool = True, changed: int = AT - 600) -> dict[str, Any]:
    """The split fields of a group state: the agent keeps it in `mode`, the uplink to FIRST."""
    return {
        "routing": "split",
        "mode": mode,
        "mode_changed_at": changed,
        "uplink": {"endpoint": FIRST.to_dict(), "alive": alive, "checked_at": AT},
        "networks": 8123,
    }


def test_a_split_group_keeps_its_mode_uplink_and_networks() -> None:
    group = reading(split=split()).groups[PORT]

    assert group.split and not group.emergency
    assert group.uplink == Uplink(LEADER, alive=True)
    assert group.networks == 8123
    assert group.mode_changed_at == AT - 600


def test_a_split_group_forwarded_by_the_agent_is_in_emergency() -> None:
    group = reading(split=split("forward", alive=False)).groups[PORT]

    assert group.emergency
    assert group.uplink == Uplink(LEADER, alive=False)


def test_an_uplink_to_an_address_of_no_member_has_no_member() -> None:
    fields = split() | {"uplink": {"endpoint": None, "alive": False, "checked_at": None}}

    assert reading(split=fields).groups[PORT].uplink == Uplink(None, alive=False)


def test_the_split_health_survives_storing() -> None:
    health = reading(split=split("forward", alive=False))

    assert RelayHealth.load(json.loads(json.dumps(health.to_dict()))) == health


def test_health_stored_before_split_routing_is_a_forwarded_group() -> None:
    stored = reading().to_dict()
    for group in stored["groups"]:
        for key in ("routing", "mode", "mode_changed_at", "uplink", "networks"):
            del group[key]

    group = RelayHealth.load(stored).groups[PORT]  # type: ignore[union-attr]

    assert group.routing is Routing.FORWARD and not group.split and group.uplink is None


def test_the_resolver_status_is_kept_and_survives_storing() -> None:
    resolver = {"status": "failed", "error": "resolver stopped with code 1"}
    text = json.dumps({"version": 1, "updated_at": AT, "groups": [], "resolver": resolver})

    health = RelayHealth.of(RelayState.parse(text), MEMBERS)

    assert health.resolver == ResolverState(ResolverStatus.FAILED, resolver["error"])
    assert RelayHealth.load(json.loads(json.dumps(health.to_dict()))) == health


def test_an_older_agent_or_reading_has_the_resolver_unknown() -> None:
    stored = reading().to_dict()
    del stored["resolver"]

    assert reading().resolver.status is ResolverStatus.UNKNOWN
    assert RelayHealth.load(stored).resolver == ResolverState()  # type: ignore[union-attr]


def test_the_agent_moving_a_split_group_to_forward_is_an_emergency() -> None:
    before = reading(split=split())
    after = reading(at=AT + 60, split=split("forward", alive=False, changed=AT + 30))

    assert list(after.changes(before)) == [ModeChanged(PORT, True, LEADER, AT + 30)]


def test_the_agent_moving_a_group_back_to_split_is_told() -> None:
    before = reading(split=split("forward", alive=False))
    after = reading(at=AT + 60, split=split(changed=AT + 50))

    assert list(after.changes(before)) == [ModeChanged(PORT, False, LEADER, AT + 50)]


def test_the_panel_moving_a_group_between_routings_is_no_mode_change() -> None:
    forwarded, split_now = reading(), reading(at=AT + 60, split=split())

    assert list(split_now.changes(forwarded)) == []
    assert list(reading(at=AT + 120).changes(split_now)) == []


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


def test_a_member_that_answers_steadily_again_is_told_once() -> None:
    health = reading(backends=[backend(FIRST), backend(SECOND, alive=False, failures=3)])
    told = []
    for at in range(AT + 60, AT + STEADY_SECONDS + 120, 60):
        health, before = reading(at=at, previous=health), health
        told += [(at, change) for change in health.changes(before)]

    assert told == [(AT + STEADY_SECONDS, MemberUp(PORT, JOINER))]


def test_a_member_that_answers_now_and_then_is_told_down_once() -> None:
    down = [backend(FIRST), backend(SECOND, alive=False, failures=3)]
    shaky = [backend(FIRST), backend(SECOND, alive=False, failures=1)]
    health = reading(backends=down)
    told = list(health.changes(reading()))
    for step in range(1, 30):
        backends = [backend(FIRST), backend(SECOND)] if step % 4 == 0 else [down, shaky][step % 2]
        health, before = reading(at=AT + step * 60, backends=backends, previous=health), health
        told += health.changes(before)

    assert told == [MemberDown(PORT, JOINER)]
    assert health.groups[PORT].lost() == {JOINER.id}


def test_an_outage_survives_storing() -> None:
    down = [backend(FIRST), backend(SECOND, alive=False, failures=3)]
    health = reading(at=AT + 60, previous=reading(backends=down))

    assert health.groups[PORT].outages == (Outage(JOINER, AT, AT),)
    assert RelayHealth.load(json.loads(json.dumps(health.to_dict()))) == health


def test_a_member_down_in_a_reading_stored_before_outages_has_one() -> None:
    down = [backend(FIRST), backend(SECOND, alive=False, failures=3)]
    stored = reading(backends=down).to_dict()
    for group in stored["groups"]:
        del group["outages"]

    before = RelayHealth.load(stored)
    assert before is not None
    assert list(reading(at=AT + 60, backends=down, previous=before).changes(before)) == []


def test_an_outage_of_a_member_that_left_ends_untold() -> None:
    down = [backend(FIRST), backend(SECOND, alive=False, failures=3)]
    before = reading(backends=down)
    after = reading(at=AT + 60, backends=[backend(FIRST)], previous=before)

    assert list(after.changes(before)) == []
    assert after.groups[PORT].outages == ()


def test_a_member_that_was_never_down_is_not_told_up() -> None:
    assert list(reading().changes(None)) == []
    assert list(reading(at=AT + 60).changes(reading())) == []


def test_a_group_that_has_a_member_again_is_told_once() -> None:
    down = [backend(FIRST, alive=False, failures=3), backend(SECOND, alive=False, failures=5)]
    gone = reading(active=None, backends=down)
    back = reading(at=AT + 60, backends=[backend(FIRST), down[1]], previous=gone)
    later = reading(at=AT + 120, backends=[backend(FIRST), down[1]], previous=back)

    assert list(back.changes(gone)) == [GroupUp(PORT)]
    assert list(later.changes(back)) == []


def test_rates_come_from_the_counters_of_two_readings() -> None:
    before = reading(sent=1000, received=4000)
    after = reading(at=AT + 10, sent=3000, received=5000)

    assert after.rates(before) == (200.0, 100.0)


def test_rates_are_unknown_without_a_previous_reading_or_after_a_restart() -> None:
    before = reading(sent=1000, received=4000)

    assert before.rates(None) is None
    assert before.rates(before) is None
    assert reading(at=AT + 10, sent=10, received=10).rates(before) is None


def direct(outgoing: int, incoming: int) -> dict[str, Any]:
    return {
        "routing": "split",
        "mode": "split",
        "bytes_direct_from_clients": outgoing,
        "bytes_direct_to_clients": incoming,
    }


def test_split_traffic_is_kept_with_the_reading() -> None:
    before = reading(sent=1000, received=1000, split=direct(0, 0))
    after = reading(at=AT + 10, sent=2000, received=3000, split=direct(500, 500), previous=before)

    stored = RelayHealth.load(json.loads(json.dumps(after.to_dict())))

    assert stored is not None
    assert stored.split_traffic == SplitTraffic(abroad=200.0, direct=100.0)
    assert stored.groups[PORT].bytes_direct_to_clients == 500


def test_a_reading_stored_before_split_traffic_has_none() -> None:
    data = reading().to_dict()
    for group in data["groups"]:
        for key in ("bytes_direct_from_clients", "bytes_direct_to_clients", "traffic"):
            del group[key]

    stored = RelayHealth.load(data)

    assert stored is not None and stored.split_traffic is None
    assert stored.groups[PORT].bytes_direct_from_clients is None


def test_a_group_of_no_backends_has_nothing_down() -> None:
    group = GroupHealth(PORT, None, None, (), (), 0, 0)

    assert not group.all_down
