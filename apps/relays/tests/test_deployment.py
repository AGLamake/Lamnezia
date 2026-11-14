"""Installing, updating and removing a relay of a group of two fake members."""

import json
from typing import Any

import pytest

from amnezia_core.awg import AWG_CONFIG_PATH, AwgServerConfig
from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.groups.models import ServerGroup
from apps.groups.tests.conftest import JOINER_HOST, LEADER_HOST, PORT, Fakes, succeeded
from apps.keys.services.drift import compare
from apps.keys.services.peers import read_registry
from apps.keys.services.service_peers import service_peers
from apps.notifications.events import Event
from apps.notifications.models import Notification
from apps.operations.models import Operation, OperationStatus
from apps.protocols.models import ProtocolInstance
from apps.protocols.tests.awg_case import OnCommit, Start, issued_key
from apps.protocols.tests.fake_awg import FakeAwgServer
from apps.relays.errors import RelayError, RelayErrorCode
from apps.relays.models import Relay, ServicePeer
from apps.relays.services import deployment
from apps.relays.services.container import FOLDER
from apps.relays.services.deployment import INSTALL_KIND, REMOVE_KIND, UPDATE_KIND, sync_relays
from apps.relays.tests.conftest import RELAY_HOST
from apps.relays.tests.fake_relay import FakeRelayServer
from apps.servers.models import Server

pytestmark = pytest.mark.django_db


def peers_of(fake: FakeAwgServer) -> set[str]:
    config = AwgServerConfig.parse(fake.container_files[AWG_CONFIG_PATH].decode())
    return {peer.public_key for peer in config.peers}


def written(relay_fake: FakeRelayServer) -> dict[str, Any]:
    config: dict[str, Any] = json.loads(relay_fake.files[f"{FOLDER}/config.json"])
    return config


def run(kind: str, server: Server, admin: User, runner: Start) -> Operation:
    return runner(lambda: deployment.start(kind, server, user=admin))


@pytest.mark.usefixtures("relayed")
def test_install_puts_the_service_peer_on_every_member(
    relay_server: Server, servers: Fakes, admin: User, start: Start
) -> None:
    succeeded(run(INSTALL_KIND, relay_server, admin, start))

    peer = ServicePeer.objects.get()
    assert peer.public_key in peers_of(servers[LEADER_HOST])
    assert peer.public_key in peers_of(servers[JOINER_HOST])
    relay = Relay.objects.get(server=relay_server)
    assert relay.installed and relay.synced and relay.synced_at


def test_install_moves_the_keys_of_the_group_to_the_relay(
    relayed: ServerGroup,
    leader: ProtocolInstance,
    relay_server: Server,
    admin: User,
    start: Start,
) -> None:
    owner = UserFactory()
    key = issued_key(leader, owner)

    succeeded(run(INSTALL_KIND, relay_server, admin, start))

    key.refresh_from_db()
    profile = json.loads(key.profile)
    assert (profile["host"], profile["port"]) == (RELAY_HOST, PORT)
    assert Notification.objects.get(event=Event.KEYS_UPDATED).user == owner


@pytest.mark.usefixtures("relayed")
def test_install_writes_the_members_in_order(
    relay_server: Server,
    relay_fake: FakeRelayServer,
    admin: User,
    start: Start,
) -> None:
    succeeded(run(INSTALL_KIND, relay_server, admin, start))

    (group,) = written(relay_fake)["groups"]
    assert group["port"] == PORT
    assert [backend["host"] for backend in group["backends"]] == [LEADER_HOST, JOINER_HOST]
    assert relay_fake.ports == {PORT}
    assert group["service"]["private_key"] == ServicePeer.objects.get().private_key


@pytest.mark.usefixtures("relayed")
def test_a_member_that_does_not_answer_falls_behind(
    relay_server: Server,
    relay_fake: FakeRelayServer,
    down: set[str],
    admin: User,
    start: Start,
) -> None:
    down.add(JOINER_HOST)

    succeeded(run(INSTALL_KIND, relay_server, admin, start))

    (group,) = written(relay_fake)["groups"]
    assert [backend["host"] for backend in group["backends"]] == [LEADER_HOST]


def test_a_reserved_port_is_refused_before_connecting(
    relayed: ServerGroup,
    relay_server: Server,
    relay_fake: FakeRelayServer,
    admin: User,
    start: Start,
) -> None:
    relayed.port = 443
    relayed.save(update_fields=["port"])

    operation = run(INSTALL_KIND, relay_server, admin, start)

    assert operation.status == OperationStatus.FAILED
    assert operation.error_code == RelayErrorCode.PORT_RESERVED
    assert not relay_fake.docker_calls


@pytest.mark.usefixtures("relayed")
def test_a_busy_port_is_refused_before_the_build(
    relay_server: Server,
    relay_fake: FakeRelayServer,
    admin: User,
    start: Start,
) -> None:
    relay_fake.others = [f"amnezia-awg2 0.0.0.0:{PORT}->{PORT}/udp"]

    operation = run(INSTALL_KIND, relay_server, admin, start)

    assert operation.error_code == RelayErrorCode.PORT_BUSY
    assert "build" not in relay_fake.docker_calls


@pytest.mark.usefixtures("relayed")
def test_the_service_peer_is_not_a_key_or_drift(
    relay_server: Server,
    leader: ProtocolInstance,
    servers: Fakes,
    admin: User,
    start: Start,
) -> None:
    succeeded(run(INSTALL_KIND, relay_server, admin, start))
    peer = ServicePeer.objects.get()

    assert service_peers.of(leader.source) == {peer.public_key}
    registry = read_registry(servers[LEADER_HOST].host())
    found = compare(registry, [], service_peers.of(leader.source))
    assert peer.public_key not in {finding.public_key for finding in found}


@pytest.mark.usefixtures("relayed")
def test_remove_drops_the_container_and_the_role(
    relay_server: Server,
    relay_fake: FakeRelayServer,
    admin: User,
    start: Start,
) -> None:
    succeeded(run(INSTALL_KIND, relay_server, admin, start))

    succeeded(run(REMOVE_KIND, relay_server, admin, start))

    assert not relay_fake.state and not Relay.objects.exists()


def test_an_update_needs_an_installed_relay(relay_server: Server, admin: User) -> None:
    with pytest.raises(RelayError) as error:
        deployment.start(UPDATE_KIND, relay_server, user=admin)

    assert error.value.code == RelayErrorCode.NOT_INSTALLED


def test_sync_updates_a_relay_whose_group_changed(  # noqa: PLR0913, PLR0917
    relayed: ServerGroup,
    relay_server: Server,
    relay_fake: FakeRelayServer,
    admin: User,
    start: Start,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    succeeded(run(INSTALL_KIND, relay_server, admin, start))
    with django_capture_on_commit_callbacks(execute=True):
        sync_relays()
    assert not Operation.objects.filter(kind=UPDATE_KIND).exists(), "nothing changed yet"

    relayed.port = 51820
    relayed.save(update_fields=["port"])
    with django_capture_on_commit_callbacks(execute=True):
        sync_relays()

    succeeded(Operation.objects.get(kind=UPDATE_KIND))
    assert relay_fake.ports == {51820}


def test_sync_waits_for_a_running_operation(
    relayed: ServerGroup, relay_server: Server, admin: User, start: Start
) -> None:
    succeeded(run(INSTALL_KIND, relay_server, admin, start))
    relayed.port = 51820
    relayed.save(update_fields=["port"])
    Operation.objects.filter(kind=INSTALL_KIND).update(status=OperationStatus.RUNNING)

    sync_relays()

    assert not Operation.objects.filter(kind=UPDATE_KIND).exists()
