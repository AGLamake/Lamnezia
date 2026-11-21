"""A relay whose config someone else replaced: the monitor notices it and a sync writes it again."""

import json
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address, IPv4Interface
from typing import Any

import pytest
from django.utils import timezone

from amnezia_core.keys import KeyPair
from amnezia_core.relay import Backend, RelayConfig, RelayGroup, RelayState, ServicePeer
from apps.accounts.models import User
from apps.groups.models import ServerGroup
from apps.groups.tests.conftest import JOINER_HOST, LEADER_HOST, PORT, succeeded
from apps.operations.models import Operation
from apps.protocols.tests.awg_case import OnCommit, Start
from apps.relays.models import Relay
from apps.relays.services import deployment
from apps.relays.services.deployment import INSTALL_KIND, UPDATE_KIND, sync_relays
from apps.relays.services.mismatch import GroupMismatch, RelayMismatch
from apps.relays.services.monitor import STATE_PATH, RelayMonitor
from apps.relays.tests.fake_relay import FakeRelayServer
from apps.servers.models import Server
from apps.ssh.services import RemoteHost

FOREIGN_PORT = 5542
FOREIGN_HOST = "198.51.100.7"
_KEYS = KeyPair.generate()
PEER = ServicePeer(
    _KEYS.private_key, IPv4Interface("10.8.1.2/32"), _KEYS.public_key, _KEYS.public_key, {}
)


def backend(host: str, port: int = PORT) -> Backend:
    return Backend(IPv4Address(host), port)


def config(*groups: tuple[int, tuple[Backend, ...]]) -> RelayConfig:
    return RelayConfig(tuple(RelayGroup(port, members, PEER) for port, members in groups))


def state(*groups: tuple[int, tuple[Backend, ...]]) -> RelayState:
    return RelayState.parse(state_text(datetime.now(UTC), *groups))


def state_text(at: datetime, *groups: tuple[int, tuple[Backend, ...]]) -> str:
    def group(port: int, members: tuple[Backend, ...]) -> dict[str, Any]:
        probed = [{**member.to_dict(), "alive": True, "failures": 0} for member in members]
        return {
            "port": port,
            "active": members[0].to_dict(),
            "backends": probed,
            "bytes_from_clients": 0,
            "bytes_to_clients": 0,
        }

    groups_data = [group(port, members) for port, members in groups]
    return json.dumps({"version": 1, "updated_at": int(at.timestamp()), "groups": groups_data})


MEMBERS = (backend(LEADER_HOST), backend(JOINER_HOST))


def test_the_same_groups_are_no_mismatch() -> None:
    assert not RelayMismatch.of(config((PORT, MEMBERS)), state((PORT, MEMBERS[::-1])))


def test_a_replaced_config_shows_foreign_and_missing_groups() -> None:
    found = RelayMismatch.of(
        config((PORT, MEMBERS)), state((FOREIGN_PORT, (backend(FOREIGN_HOST, FOREIGN_PORT),)))
    )

    assert found.foreign_ports == (FOREIGN_PORT,)
    assert found.missing_ports == (PORT,)
    assert found.groups == ()


def test_other_members_of_a_group_are_named() -> None:
    found = RelayMismatch.of(
        config((PORT, MEMBERS)), state((PORT, (backend(LEADER_HOST), backend(FOREIGN_HOST))))
    )

    assert found.groups == (GroupMismatch(PORT, (backend(FOREIGN_HOST),), (backend(JOINER_HOST),)),)
    assert RelayMismatch.load(found.to_dict()) == found


def test_nothing_or_an_unknown_form_loads_as_no_mismatch() -> None:
    assert RelayMismatch().to_dict() == {}
    assert not RelayMismatch.load({})
    assert not RelayMismatch.load({"groups": [{"port": "x"}]})


def test_the_same_published_ports_in_any_order_are_no_mismatch() -> None:
    same = RelayMismatch.of(config((PORT, MEMBERS)), state((PORT, MEMBERS)))

    assert not same.with_published([PORT, FOREIGN_PORT], frozenset({FOREIGN_PORT, PORT}))


def test_missing_and_extra_published_ports_are_a_mismatch() -> None:
    found = RelayMismatch.of(config((PORT, MEMBERS)), state((PORT, MEMBERS))).with_published(
        [PORT], [FOREIGN_PORT]
    )

    assert found
    assert found.unpublished_ports == (PORT,)
    assert found.extra_published_ports == (FOREIGN_PORT,)
    assert RelayMismatch.load(found.to_dict()) == found


def test_a_mismatch_stored_before_the_ports_were_compared_still_loads() -> None:
    stored = {"foreign_ports": [FOREIGN_PORT], "missing_ports": [], "groups": []}

    assert RelayMismatch.load(stored) == RelayMismatch(foreign_ports=(FOREIGN_PORT,))


# The monitor and the sync on an installed relay.


class Agent:
    """Writes the state file the way the agent would, after whatever the panel did so far."""

    def __init__(self, fake: FakeRelayServer) -> None:
        self._fake = fake
        self.now = timezone.now()
        self.connections = 0

    def writes(self, *groups: tuple[int, tuple[Backend, ...]]) -> None:
        self.now = timezone.now() + timedelta(seconds=1)
        self._fake.files[STATE_PATH] = state_text(self.now, *groups).encode()

    def poll(self, relay: Relay) -> Relay:
        """Poll the relay as the task does, loaded afresh, and return it as kept."""
        relay.refresh_from_db()
        RelayMonitor(timedelta(seconds=60), connect=self._connect, clock=lambda: self.now).poll(
            relay
        )
        relay.refresh_from_db()
        return relay

    @property
    def _connect(self) -> Callable[[Server], AbstractContextManager[RemoteHost]]:
        @contextmanager
        def connect(_server: Server) -> Iterator[RemoteHost]:
            self.connections += 1
            yield self._fake.host()

        return connect


@pytest.fixture
def installed(relayed: ServerGroup, relay_server: Server, admin: User, start: Start) -> Relay:
    succeeded(start(lambda: deployment.start(INSTALL_KIND, relay_server, user=admin)))
    return Relay.objects.get(server=relay_server)


@pytest.fixture
def agent(relay_fake: FakeRelayServer) -> Agent:
    return Agent(relay_fake)


FOREIGN = (FOREIGN_PORT, (backend(FOREIGN_HOST, FOREIGN_PORT),))


@pytest.mark.django_db
def test_a_relay_that_forwards_the_panel_groups_has_no_mismatch(
    installed: Relay, agent: Agent
) -> None:
    synced = installed.synced
    agent.writes((PORT, MEMBERS))

    relay = agent.poll(installed)

    assert not relay.settings_mismatch
    assert relay.synced == synced


@pytest.mark.django_db
def test_a_replaced_config_is_written_again_by_the_next_sync(
    installed: Relay,
    agent: Agent,
    relay_fake: FakeRelayServer,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    # Someone started the container on another port with another group.
    relay_fake.ports = {FOREIGN_PORT}
    agent.writes(FOREIGN)

    relay = agent.poll(installed)

    assert relay.settings_mismatch == RelayMismatch(
        (FOREIGN_PORT,), (PORT,), unpublished_ports=(PORT,), extra_published_ports=(FOREIGN_PORT,)
    )
    assert relay.synced == ""
    assert [health.port for health in relay.groups_health] == []

    with django_capture_on_commit_callbacks(execute=True):
        sync_relays()
    succeeded(Operation.objects.get(kind=UPDATE_KIND))
    assert relay_fake.ports == {PORT}, "the container publishes the port of the group again"

    agent.writes((PORT, MEMBERS))
    relay = agent.poll(relay)
    assert not relay.settings_mismatch
    assert [health.port for health in relay.groups_health] == [PORT]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "ports",
    [
        (set(), RelayMismatch(unpublished_ports=(PORT,))),
        ({PORT, FOREIGN_PORT}, RelayMismatch(extra_published_ports=(FOREIGN_PORT,))),
    ],
    ids=["missing", "foreign"],
)
def test_a_container_on_other_ports_is_started_again_by_the_next_sync(
    installed: Relay,
    agent: Agent,
    relay_fake: FakeRelayServer,
    django_capture_on_commit_callbacks: OnCommit,
    ports: tuple[set[int], RelayMismatch],
) -> None:
    # The agent forwards the panel's group, but the container was started on other ports.
    relay_fake.ports, expected = ports
    agent.writes((PORT, MEMBERS))

    relay = agent.poll(installed)

    assert relay.settings_mismatch == expected
    assert relay.synced == ""
    assert agent.connections == 1, "the ports are read over the connection of the state"

    relay_fake.docker_calls.clear()
    with django_capture_on_commit_callbacks(execute=True):
        sync_relays()
    succeeded(Operation.objects.get(kind=UPDATE_KIND))
    assert "run" in relay_fake.docker_calls, "the update started the container again"
    assert relay_fake.ports == {PORT}

    agent.writes((PORT, MEMBERS))
    assert not agent.poll(relay).settings_mismatch


@pytest.mark.django_db
def test_ports_docker_does_not_tell_leave_the_state_judged(
    installed: Relay, agent: Agent, relay_fake: FakeRelayServer
) -> None:
    synced = installed.synced
    relay_fake.ports, relay_fake.inspect_fails = set(), True
    agent.writes((PORT, MEMBERS))

    relay = agent.poll(installed)

    assert relay.read_error == ""
    assert relay.read_at is not None
    assert not relay.settings_mismatch, "unknown ports are no mismatch"
    assert relay.synced == synced


@pytest.mark.django_db
def test_a_mismatch_that_stays_after_the_update_is_not_updated_again(
    installed: Relay, agent: Agent, django_capture_on_commit_callbacks: OnCommit
) -> None:
    agent.writes(FOREIGN)
    agent.poll(installed)
    with django_capture_on_commit_callbacks(execute=True):
        sync_relays()

    agent.writes(FOREIGN)
    relay = agent.poll(installed)

    assert relay.settings_mismatch, "the card still shows it"
    assert relay.synced, "no second update by itself"


@pytest.mark.django_db
def test_a_mismatch_is_not_judged_while_an_update_is_due(
    installed: Relay, agent: Agent, relayed: ServerGroup
) -> None:
    relayed.port = 51820
    relayed.save(update_fields=["port"])
    agent.writes((PORT, MEMBERS))

    relay = agent.poll(installed)

    assert not relay.settings_mismatch


@pytest.mark.django_db
def test_a_state_written_before_the_last_update_is_not_judged(
    installed: Relay, agent: Agent
) -> None:
    agent.writes(FOREIGN)
    Relay.objects.filter(pk=installed.pk).update(synced_at=agent.now + timedelta(seconds=1))
    installed.refresh_from_db()

    relay = agent.poll(installed)

    assert not relay.settings_mismatch
    assert relay.synced
