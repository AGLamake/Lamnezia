"""The routing of a group behind a relay: the uplink peer, the clone and the change of mode."""

import json
from typing import Any

import pytest

from amnezia_core.awg import AWG_CONFIG_PATH, AwgServerConfig
from apps.accounts.models import User
from apps.audit.models import AuditEvent
from apps.groups.models import GroupListMode, GroupMember, GroupRouting, ServerGroup
from apps.groups.services.replication import catch_up_lagging
from apps.groups.tests.conftest import JOINER_HOST, LEADER_HOST, Fakes, succeeded
from apps.keys.models import AccessKey
from apps.keys.services.actions import KeyActions
from apps.operations.models import Operation, OperationStatus
from apps.protocols.models import ProtocolInstance
from apps.protocols.tests.awg_case import OnCommit, Start, issued_key
from apps.protocols.tests.fake_awg import FakeAwgServer
from apps.relays.errors import RelayError, RelayErrorCode
from apps.relays.models import PeerRole, Relay, ServicePeer
from apps.relays.services import deployment
from apps.relays.services.container import FOLDER
from apps.relays.services.deployment import (
    INSTALL_KIND,
    PEERS_KIND,
    ROUTING_KIND,
    UPDATE_KIND,
    sync_relays,
)
from apps.relays.services.image import IMAGE_VERSION, agent_digest
from apps.relays.services.routing import start_routing
from apps.relays.tests.fake_relay import FakeRelayServer
from apps.servers.models import Server

pytestmark = pytest.mark.django_db

PEERS = f"{FOLDER}/peers-55424.conf"


def peers_of(fake: FakeAwgServer) -> set[str]:
    config = AwgServerConfig.parse(fake.container_files[AWG_CONFIG_PATH].decode())
    return {peer.public_key for peer in config.peers}


def written_group(relay_fake: FakeRelayServer) -> dict[str, Any]:
    (group,) = json.loads(relay_fake.files[f"{FOLDER}/config.json"])["groups"]
    result: dict[str, Any] = group
    return result


def uplink() -> ServicePeer:
    return ServicePeer.objects.get(role=PeerRole.UPLINK)


@pytest.fixture
def installed(relayed: ServerGroup, relay_server: Server, admin: User, start: Start) -> ServerGroup:
    succeeded(start(lambda: deployment.start(INSTALL_KIND, relay_server, user=admin)))
    return relayed


def route(group: ServerGroup, routing: GroupRouting, admin: User, start: Start) -> Operation:
    return start(lambda: start_routing(group, routing, user=admin, ip="198.51.100.7"))


def test_split_gives_every_member_the_uplink_and_the_relay_a_clone(  # noqa: PLR0913, PLR0917
    installed: ServerGroup,
    leader: ProtocolInstance,
    servers: Fakes,
    relay_fake: FakeRelayServer,
    admin: User,
    start: Start,
) -> None:
    key = issued_key(leader)

    succeeded(route(installed, GroupRouting.SPLIT, admin, start))

    installed.refresh_from_db()
    assert installed.routing == GroupRouting.SPLIT
    peer = uplink()
    assert peer.public_key in peers_of(servers[LEADER_HOST])
    assert peer.public_key in peers_of(servers[JOINER_HOST])
    group = written_group(relay_fake)
    assert group["routing"] == "split"
    assert group["split"]["clone"]["address"] == "10.8.1.0/24"
    assert group["split"]["clone"]["private_key"] == leader.private_key
    assert group["split"]["uplink"]["private_key"] == peer.private_key
    assert group["split"]["dns"] == list(leader.dns)
    assert group["split"]["list_mode"] == "exclude"
    assert f"PublicKey = {key.public_key}" in relay_fake.files[PEERS].decode()


def test_the_relay_gets_the_list_mode_of_the_group(
    installed: ServerGroup,
    leader: ProtocolInstance,
    relay_fake: FakeRelayServer,
    admin: User,
    start: Start,
) -> None:
    installed.list_mode = GroupListMode.INCLUDE
    installed.save()

    succeeded(route(installed, GroupRouting.SPLIT, admin, start))

    assert written_group(relay_fake)["split"]["list_mode"] == "include"
    assert not installed.split_sources.exists()


def test_forward_again_drops_the_clone_and_then_the_uplink(
    installed: ServerGroup,
    servers: Fakes,
    relay_fake: FakeRelayServer,
    admin: User,
    start: Start,
) -> None:
    succeeded(route(installed, GroupRouting.SPLIT, admin, start))
    public_key = uplink().public_key

    succeeded(route(installed, GroupRouting.FORWARD, admin, start))

    group = written_group(relay_fake)
    assert group["routing"] == "forward" and "split" not in group
    assert PEERS not in relay_fake.files
    assert not ServicePeer.objects.filter(role=PeerRole.UPLINK).exists()
    assert ServicePeer.objects.filter(role=PeerRole.PROBE).exists()
    assert public_key not in peers_of(servers[LEADER_HOST])
    assert public_key not in peers_of(servers[JOINER_HOST])


def test_a_member_down_at_the_split_gets_the_uplink_on_its_catch_up(  # noqa: PLR0913, PLR0917
    installed: ServerGroup,
    joined: GroupMember,
    servers: Fakes,
    down: set[str],
    admin: User,
    start: Start,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    down.add(JOINER_HOST)

    succeeded(route(installed, GroupRouting.SPLIT, admin, start))

    joined.refresh_from_db()
    assert joined.lagging and uplink().public_key not in peers_of(servers[JOINER_HOST])
    down.clear()
    with django_capture_on_commit_callbacks(execute=True):
        catch_up_lagging()
    joined.refresh_from_db()
    assert not joined.lagging and uplink().public_key in peers_of(servers[JOINER_HOST])


def test_a_leader_down_fails_the_split_before_it_is_saved(
    installed: ServerGroup, relay_fake: FakeRelayServer, down: set[str], admin: User, start: Start
) -> None:
    before = written_group(relay_fake)
    down.add(LEADER_HOST)

    operation = route(installed, GroupRouting.SPLIT, admin, start)

    assert operation.status == OperationStatus.FAILED
    installed.refresh_from_db()
    assert installed.routing == GroupRouting.FORWARD
    assert written_group(relay_fake) == before


def test_the_change_reads_the_counters_first(
    installed: ServerGroup, admin: User, start: Start, monkeypatch: pytest.MonkeyPatch
) -> None:
    polled: list[str] = []
    monkeypatch.setattr(
        "apps.relays.services.routing.TrafficPoller.poll",
        lambda self, target: polled.append(target.name),
    )

    succeeded(route(installed, GroupRouting.SPLIT, admin, start))

    assert len(set(polled)) == 2


def test_the_change_is_logged_in_the_audit(
    installed: ServerGroup, admin: User, start: Start
) -> None:
    operation = route(installed, GroupRouting.SPLIT, admin, start)

    event = AuditEvent.objects.get(action=ROUTING_KIND)
    assert event.data == {"operation": operation.pk, "routing": "split"}
    assert event.ip == "198.51.100.7"


def test_a_group_without_an_installed_relay_is_refused(group: ServerGroup, admin: User) -> None:
    with pytest.raises(RelayError) as error:
        start_routing(group, GroupRouting.SPLIT, user=admin)

    assert error.value.code == RelayErrorCode.NOT_INSTALLED


def test_the_same_routing_is_refused(installed: ServerGroup, admin: User) -> None:
    with pytest.raises(RelayError) as error:
        start_routing(installed, GroupRouting.FORWARD, user=admin)

    assert error.value.code == RelayErrorCode.ROUTING_SAME


def test_the_install_records_the_image_and_the_agent(installed: ServerGroup) -> None:
    relay = Relay.objects.get(server=installed.relay)

    assert (relay.image_version, relay.agent_digest) == (IMAGE_VERSION, agent_digest())
    assert relay.split_refusal is None


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"image_version": IMAGE_VERSION - 1}, RelayErrorCode.SPLIT_IMAGE_OUTDATED),
        ({"agent_digest": ""}, RelayErrorCode.SPLIT_AGENT_OUTDATED),
    ],
)
def test_a_split_on_an_older_relay_is_refused(
    installed: ServerGroup, admin: User, change: dict[str, Any], code: RelayErrorCode
) -> None:
    Relay.objects.update(**change)

    with pytest.raises(RelayError) as error:
        start_routing(installed, GroupRouting.SPLIT, user=admin)

    assert error.value.code == code
    assert not Operation.objects.filter(kind=ROUTING_KIND).exists()


def test_forward_again_on_an_older_image_is_allowed(installed: ServerGroup, admin: User) -> None:
    ServerGroup.objects.filter(pk=installed.pk).update(routing=GroupRouting.SPLIT)
    Relay.objects.update(image_version=0, agent_digest="")

    start_routing(installed, GroupRouting.FORWARD, user=admin)

    assert Operation.objects.filter(kind=ROUTING_KIND).exists()


def test_an_update_records_the_agent(
    installed: ServerGroup, relay_server: Server, admin: User, start: Start
) -> None:
    Relay.objects.update(agent_digest="")

    succeeded(start(lambda: deployment.start(UPDATE_KIND, relay_server, user=admin)))

    assert Relay.objects.get(server=installed.relay).agent_digest == agent_digest()


def test_a_busy_relay_is_refused(installed: ServerGroup, admin: User) -> None:
    Operation.objects.filter(kind=INSTALL_KIND).update(status=OperationStatus.RUNNING)

    with pytest.raises(RelayError) as error:
        start_routing(installed, GroupRouting.SPLIT, user=admin)

    assert error.value.code == RelayErrorCode.RELAY_BUSY
    assert not Operation.objects.filter(kind=ROUTING_KIND).exists()


def test_an_issued_key_reaches_the_clone(
    installed: ServerGroup,
    leader: ProtocolInstance,
    relay_fake: FakeRelayServer,
    admin: User,
    start: Start,
) -> None:
    succeeded(route(installed, GroupRouting.SPLIT, admin, start))

    operation = start(lambda: KeyActions(admin).issue(leader.source, "phone", None))

    succeeded(operation)
    key = AccessKey.objects.get(pk=operation.params["key_id"])
    assert f"PublicKey = {key.public_key}" in relay_fake.files[PEERS].decode()
    succeeded(Operation.objects.filter(kind=PEERS_KIND).latest("pk"))


def test_a_revoked_key_leaves_the_clone(
    installed: ServerGroup,
    leader: ProtocolInstance,
    relay_fake: FakeRelayServer,
    admin: User,
    start: Start,
) -> None:
    key = issued_key(leader)
    succeeded(route(installed, GroupRouting.SPLIT, admin, start))

    succeeded(start(lambda: KeyActions(admin).revoke(key)))

    assert key.public_key not in relay_fake.files[PEERS].decode()


def test_the_minute_sync_writes_peers_the_signals_missed(  # noqa: PLR0913, PLR0917
    installed: ServerGroup,
    leader: ProtocolInstance,
    relay_fake: FakeRelayServer,
    admin: User,
    start: Start,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    succeeded(route(installed, GroupRouting.SPLIT, admin, start))
    key = issued_key(leader)
    AccessKey.objects.filter(pk=key.pk).update(name="renamed")
    relay_fake.files[PEERS] = b""

    with django_capture_on_commit_callbacks(execute=True):
        sync_relays()

    assert f"PublicKey = {key.public_key}" in relay_fake.files[PEERS].decode()
    assert not Operation.objects.filter(kind=deployment.UPDATE_KIND).exists()
