"""Traffic, online status and limits of a split group from its clone on the relay."""

import pytest

from apps.accounts.models import User
from apps.groups.models import GroupRouting, ServerGroup
from apps.groups.tests.conftest import succeeded
from apps.keys.models import AccessKey
from apps.keys.services.actions import KeyActions
from apps.keys.services.limits import disable_by_limit
from apps.operations.models import Operation, OperationStatus
from apps.protocols.models import ProtocolInstance
from apps.protocols.tests.awg_case import Start, issued_key
from apps.relays.models import PeerRole, ServicePeer
from apps.relays.services import deployment
from apps.relays.services.container import FOLDER, NAME
from apps.relays.services.deployment import INSTALL_KIND, UPDATE_KIND
from apps.relays.services.routing import start_routing
from apps.relays.services.traffic import relay_target_name
from apps.relays.tests.conftest import RELAY_HOST
from apps.relays.tests.fake_relay import FakeRelayServer
from apps.servers.models import Server
from apps.traffic.models import TrafficCounter
from apps.traffic.services.targets import peer_owners, poll_targets

pytestmark = pytest.mark.django_db

PEERS = f"{FOLDER}/peers-55424.conf"
SPEED = f"{FOLDER}/speed-55424.sh"


@pytest.fixture
def installed(relayed: ServerGroup, relay_server: Server, admin: User, start: Start) -> ServerGroup:
    succeeded(start(lambda: deployment.start(INSTALL_KIND, relay_server, user=admin)))
    return relayed


def route(group: ServerGroup, routing: GroupRouting, admin: User, start: Start) -> Operation:
    operation = start(lambda: start_routing(group, routing, user=admin, ip="198.51.100.7"))
    succeeded(operation)
    return operation


def clone_counter(group: ServerGroup, key: AccessKey) -> TrafficCounter:
    return TrafficCounter.objects.get(source=f"clone:{group.pk}", public_key=key.public_key)


def test_the_clone_is_polled_for_the_keys_of_the_leader(
    installed: ServerGroup,
    leader: ProtocolInstance,
    relay_server: Server,
    admin: User,
    start: Start,
) -> None:
    route(installed, GroupRouting.SPLIT, admin, start)

    target = poll_targets.get(relay_target_name(relay_server))

    assert target is not None
    source = f"clone:{installed.pk}"
    assert target.containers == {source: NAME}
    assert target.key_sources == {source: leader.source}
    assert target.interfaces == {source: "lg55424"}


def test_a_forwarded_group_has_no_clone_to_poll(
    installed: ServerGroup, relay_server: Server
) -> None:
    assert poll_targets.get(relay_target_name(relay_server)) is None


def test_the_uplink_counts_for_the_exit_load(
    installed: ServerGroup, admin: User, start: Start
) -> None:
    route(installed, GroupRouting.SPLIT, admin, start)

    uplink = ServicePeer.objects.get(role=PeerRole.UPLINK)
    assert dict(peer_owners)[uplink.public_key] == f"uplink:{installed.pk}"
    assert uplink.public_key in peer_owners.public_keys(), "not a client of the members"


def test_the_keys_count_on_the_clone_from_the_first_byte(
    installed: ServerGroup, leader: ProtocolInstance, admin: User, start: Start
) -> None:
    key = issued_key(leader)

    route(installed, GroupRouting.SPLIT, admin, start)

    counter = clone_counter(installed, key)
    assert (counter.key_source, counter.rx_bytes, counter.tx_bytes) == (leader.source, 0, 0)


def test_a_new_key_counts_on_the_clone_from_the_first_byte(
    installed: ServerGroup, leader: ProtocolInstance, admin: User, start: Start
) -> None:
    route(installed, GroupRouting.SPLIT, admin, start)

    operation = start(lambda: KeyActions(admin).issue(leader.source, "phone", None))

    key = AccessKey.objects.get(pk=operation.params["key_id"])
    assert clone_counter(installed, key).key_source == leader.source


def test_forward_again_leaves_the_clone_counters_at_zero(
    installed: ServerGroup, leader: ProtocolInstance, admin: User, start: Start
) -> None:
    key = issued_key(leader)
    route(installed, GroupRouting.SPLIT, admin, start)
    TrafficCounter.objects.filter(source=f"clone:{installed.pk}").update(
        rx_bytes=500, tx_bytes=900, present=True
    )

    route(installed, GroupRouting.FORWARD, admin, start)

    counter = clone_counter(installed, key)
    assert (counter.rx_bytes, counter.tx_bytes, counter.present) == (0, 0, False)


def test_the_change_back_reads_the_clone_first(
    installed: ServerGroup,
    relay_server: Server,
    admin: User,
    start: Start,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route(installed, GroupRouting.SPLIT, admin, start)
    polled: list[str] = []
    monkeypatch.setattr(
        "apps.relays.services.routing.TrafficPoller.poll",
        lambda self, target: polled.append(target.name),
    )

    route(installed, GroupRouting.FORWARD, admin, start)

    assert relay_target_name(relay_server) in polled


def test_a_key_over_its_traffic_limit_leaves_the_clone(
    installed: ServerGroup,
    leader: ProtocolInstance,
    relay_fake: FakeRelayServer,
    admin: User,
    start: Start,
) -> None:
    key = issued_key(leader, owner=admin)
    route(installed, GroupRouting.SPLIT, admin, start)
    assert key.public_key in relay_fake.files[PEERS].decode()

    succeeded(start(lambda: disable_by_limit(admin, "traffic")[0]))

    assert key.public_key not in relay_fake.files[PEERS].decode()


def test_the_clone_gets_the_speed_limits_of_the_leader(
    installed: ServerGroup,
    leader: ProtocolInstance,
    relay_fake: FakeRelayServer,
    admin: User,
    start: Start,
) -> None:
    issued_key(leader)
    ProtocolInstance.objects.filter(pk=leader.pk).update(download_limit=50)

    route(installed, GroupRouting.SPLIT, admin, start)

    script = relay_fake.files[SPEED].decode()
    assert "dev=lg55424" in script
    assert "rate 50mbit ceil 50mbit" in script
    assert "match ip dst 10.8.1.5/32" in script


def test_forward_again_drops_the_speed_script(
    installed: ServerGroup, relay_fake: FakeRelayServer, admin: User, start: Start
) -> None:
    route(installed, GroupRouting.SPLIT, admin, start)
    assert SPEED in relay_fake.files

    route(installed, GroupRouting.FORWARD, admin, start)

    assert SPEED not in relay_fake.files


@pytest.fixture
def polled(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The names of the targets read, with no reading taken."""
    names: list[str] = []
    monkeypatch.setattr(
        "apps.traffic.services.poller.TrafficPoller.poll",
        lambda self, target: names.append(target.name),
    )
    return names


@pytest.fixture
def forward_failed(  # noqa: PLR0913, PLR0917
    installed: ServerGroup,
    leader: ProtocolInstance,
    down: set[str],
    admin: User,
    start: Start,
    polled: list[str],
) -> AccessKey:
    """A split group saved as forwarded while the relay did not answer; its key."""
    key = issued_key(leader)
    route(installed, GroupRouting.SPLIT, admin, start)
    TrafficCounter.objects.filter(source=f"clone:{installed.pk}").update(
        rx_bytes=500, tx_bytes=900, present=True
    )
    down.add(RELAY_HOST)
    operation = start(lambda: start_routing(installed, GroupRouting.FORWARD, user=admin))
    assert operation.status == OperationStatus.FAILED
    down.discard(RELAY_HOST)
    return key


def test_a_failed_forward_keeps_the_clone_polled(
    forward_failed: AccessKey, installed: ServerGroup, relay_server: Server
) -> None:
    target = poll_targets.get(relay_target_name(relay_server))

    assert target is not None
    assert f"clone:{installed.pk}" in target.containers
    counter = clone_counter(installed, forward_failed)
    assert (counter.rx_bytes, counter.tx_bytes) == (500, 900)


def test_the_update_that_forwards_reads_the_clone_first(  # noqa: PLR0913, PLR0917
    forward_failed: AccessKey,
    installed: ServerGroup,
    relay_server: Server,
    admin: User,
    start: Start,
    polled: list[str],
) -> None:
    polled.clear()

    succeeded(start(lambda: deployment.start(UPDATE_KIND, relay_server, user=admin)))

    assert polled == [relay_target_name(relay_server)]
    assert poll_targets.get(relay_target_name(relay_server)) is None
    counter = clone_counter(installed, forward_failed)
    assert (counter.rx_bytes, counter.tx_bytes, counter.present) == (0, 0, False)
