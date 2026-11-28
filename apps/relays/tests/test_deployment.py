"""Installing, updating and removing a relay of a group of two fake members."""

import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone
from pytest import MonkeyPatch

from amnezia_core.awg import AWG_CONFIG_PATH, AwgServerConfig
from amnezia_core.keys import KeyPair
from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.groups.models import GroupMember, GroupRouting, ServerGroup
from apps.groups.services import replication
from apps.groups.services.addressing import GroupKeyAddress
from apps.groups.services.replication import catch_up_lagging
from apps.groups.tests.conftest import JOINER_HOST, LEADER_HOST, PORT, Fakes, succeeded
from apps.keys.models import AccessKey
from apps.keys.services.actions import KeyActions
from apps.keys.services.drift import compare
from apps.keys.services.peers import PeerManager, read_registry
from apps.keys.services.service_peers import service_peers
from apps.notifications.events import Event
from apps.notifications.models import Notification
from apps.operations.models import Operation, OperationStatus
from apps.protocols.models import ProtocolInstance
from apps.protocols.tests.awg_case import OnCommit, Start, issued_key
from apps.protocols.tests.fake_awg import FakeAwgServer
from apps.relays.errors import RelayError, RelayErrorCode
from apps.relays.models import PeerRole, Relay, ServicePeer, SyncSkip
from apps.relays.services import config, deployment, image
from apps.relays.services.container import FOLDER
from apps.relays.services.deployment import INSTALL_KIND, REMOVE_KIND, UPDATE_KIND, sync_relays
from apps.relays.tests.conftest import RELAY_HOST
from apps.relays.tests.fake_relay import FakeRelayServer
from apps.servers.models import Server, ServerSample
from apps.ssh.errors import ErrorCode, ServerLockError

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


def test_a_key_issued_while_the_relay_installs_gets_its_address(  # noqa: PLR0913, PLR0917
    relayed: ServerGroup,
    leader: ProtocolInstance,
    relay_server: Server,
    admin: User,
    start: Start,
    monkeypatch: MonkeyPatch,
) -> None:
    """A key saved with the address of the server after the relay counts as installed, but
    before its keys are moved, is moved too."""
    issued: list[AccessKey] = []

    def racing(group: ServerGroup, host: str | None = None) -> GroupKeyAddress:
        move = GroupKeyAddress(group, host)
        key = issued_key(leader, UserFactory())
        key.profile = json.dumps({**json.loads(key.profile), "host": LEADER_HOST}, sort_keys=True)
        key.save(update_fields=["profile"])
        issued.append(key)
        return move

    monkeypatch.setattr(deployment, "GroupKeyAddress", racing)

    succeeded(run(INSTALL_KIND, relay_server, admin, start))

    (key,) = issued
    key.refresh_from_db()
    profile = json.loads(key.profile)
    assert (profile["host"], profile["port"]) == (RELAY_HOST, PORT)


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
    relay_fake.others = [f"telemt 0.0.0.0:{PORT}->{PORT}/udp"]

    operation = run(INSTALL_KIND, relay_server, admin, start)

    assert operation.error_code == RelayErrorCode.PORT_BUSY
    assert "build" not in relay_fake.docker_calls


@pytest.mark.usefixtures("relayed")
def test_a_port_of_amnezia_wg_on_the_relay_server_says_what_to_do(
    relay_server: Server,
    relay_fake: FakeRelayServer,
    admin: User,
    start: Start,
) -> None:
    relay_fake.others = [f"amnezia-awg2 0.0.0.0:{PORT}->{PORT}/udp"]

    operation = run(INSTALL_KIND, relay_server, admin, start)

    assert operation.error_code == RelayErrorCode.PORT_AWG
    assert "another server" in operation.error_hint
    assert "build" not in relay_fake.docker_calls


def test_a_member_on_the_relay_server_is_refused_before_connecting(
    relayed: ServerGroup,
    leader: ProtocolInstance,
    relay_fake: FakeRelayServer,
    admin: User,
    start: Start,
) -> None:
    relayed.relay = leader.server
    relayed.save(update_fields=["relay"])

    operation = run(INSTALL_KIND, leader.server, admin, start)

    assert operation.status == OperationStatus.FAILED
    assert operation.error_code == RelayErrorCode.MEMBER_ON_RELAY
    assert not relay_fake.docker_calls


def test_sync_leaves_a_relay_with_a_member_on_its_server(
    relayed: ServerGroup,
    leader: ProtocolInstance,
    relay_server: Server,
    admin: User,
    start: Start,
) -> None:
    succeeded(run(INSTALL_KIND, relay_server, admin, start))
    leader.server = relay_server
    leader.save(update_fields=["server"])

    sync_relays()

    assert not Operation.objects.filter(kind=UPDATE_KIND).exists()


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


def test_remove_moves_the_keys_back_to_the_leading_server(
    relayed: ServerGroup,
    leader: ProtocolInstance,
    relay_server: Server,
    admin: User,
    start: Start,
) -> None:
    owner = UserFactory()
    key = issued_key(leader, owner)
    succeeded(run(INSTALL_KIND, relay_server, admin, start))
    Notification.objects.all().delete()

    succeeded(run(REMOVE_KIND, relay_server, admin, start))

    key.refresh_from_db()
    profile = json.loads(key.profile)
    assert (profile["host"], profile["port"]) == (LEADER_HOST, PORT)
    assert Notification.objects.get(event=Event.KEYS_UPDATED).user == owner


@pytest.mark.usefixtures("relayed")
def test_remove_takes_the_service_peer_off_the_members(
    relay_server: Server, servers: Fakes, admin: User, start: Start
) -> None:
    succeeded(run(INSTALL_KIND, relay_server, admin, start))
    public_key = ServicePeer.objects.get().public_key

    succeeded(run(REMOVE_KIND, relay_server, admin, start))

    assert public_key not in peers_of(servers[LEADER_HOST])
    assert public_key not in peers_of(servers[JOINER_HOST])
    assert not ServicePeer.objects.exists()


@pytest.mark.usefixtures("relayed")
def test_a_member_down_at_removal_loses_the_service_peer_on_its_catch_up(  # noqa: PLR0913, PLR0917
    relay_server: Server,
    joined: GroupMember,
    servers: Fakes,
    down: set[str],
    admin: User,
    start: Start,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    succeeded(run(INSTALL_KIND, relay_server, admin, start))
    public_key = ServicePeer.objects.get().public_key
    down.add(JOINER_HOST)

    succeeded(run(REMOVE_KIND, relay_server, admin, start))
    joined.refresh_from_db()
    assert joined.lagging and public_key in peers_of(servers[JOINER_HOST])

    down.clear()
    with django_capture_on_commit_callbacks(execute=True):
        catch_up_lagging()

    joined.refresh_from_db()
    assert not joined.lagging and public_key not in peers_of(servers[JOINER_HOST])


@pytest.mark.usefixtures("relayed")
def test_the_service_peer_stays_when_no_member_answers_at_removal(
    relay_server: Server, down: set[str], admin: User, start: Start
) -> None:
    succeeded(run(INSTALL_KIND, relay_server, admin, start))
    down.update({LEADER_HOST, JOINER_HOST})

    succeeded(run(REMOVE_KIND, relay_server, admin, start))

    assert ServicePeer.objects.exists() and not Relay.objects.exists()


def test_a_key_never_takes_the_address_of_the_service_peer(
    relayed: ServerGroup, leader: ProtocolInstance, admin: User, start: Start
) -> None:
    """The service peer is saved before every member has it: a key issued meanwhile on such a
    member must not take its address."""

    def issue(name: str) -> AccessKey:
        operation = start(lambda: KeyActions(admin).issue(leader.source, name, None))
        succeeded(operation)
        return AccessKey.objects.get(pk=operation.params["key_id"])

    probe = issue("probe")
    assert probe.client_ip
    succeeded(start(lambda: KeyActions(admin).revoke(probe)))
    pair = KeyPair.generate()
    ServicePeer.objects.create(
        group=relayed,
        private_key=pair.private_key,
        public_key=pair.public_key,
        address=probe.client_ip,
    )

    assert issue("phone").client_ip != probe.client_ip


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


@pytest.mark.usefixtures("relayed")
def test_sync_updates_a_relay_once_after_a_new_agent(  # noqa: PLR0913, PLR0917
    relay_server: Server,
    relay_fake: FakeRelayServer,
    admin: User,
    start: Start,
    monkeypatch: MonkeyPatch,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    succeeded(run(INSTALL_KIND, relay_server, admin, start))
    installed = Relay.objects.get(server=relay_server).synced
    with django_capture_on_commit_callbacks(execute=True):
        sync_relays()
    assert not Operation.objects.filter(kind=UPDATE_KIND).exists(), "the digest is stable"

    new_agent = b"# the agent of a new panel version\n"
    monkeypatch.setattr(image, "agent_source", lambda: new_agent)
    with django_capture_on_commit_callbacks(execute=True):
        sync_relays()
    succeeded(Operation.objects.get(kind=UPDATE_KIND))
    assert relay_fake.files[f"{FOLDER}/agent.py"] == new_agent
    assert Relay.objects.get(server=relay_server).synced != installed

    with django_capture_on_commit_callbacks(execute=True):
        sync_relays()
    assert Operation.objects.filter(kind=UPDATE_KIND).count() == 1, "one update per version"


def test_sync_waits_for_a_running_operation(
    relayed: ServerGroup, relay_server: Server, admin: User, start: Start
) -> None:
    succeeded(run(INSTALL_KIND, relay_server, admin, start))
    relayed.port = 51820
    relayed.save(update_fields=["port"])
    Operation.objects.filter(kind=INSTALL_KIND).update(status=OperationStatus.RUNNING)

    sync_relays()

    assert not Operation.objects.filter(kind=UPDATE_KIND).exists()


@pytest.mark.usefixtures("relayed")
def test_an_update_writes_the_settings_of_the_relay(
    relay_server: Server, relay_fake: FakeRelayServer, admin: User, start: Start
) -> None:
    succeeded(run(INSTALL_KIND, relay_server, admin, start))
    Relay.objects.filter(server=relay_server).update(check_interval=2, failures=4)

    succeeded(run(UPDATE_KIND, relay_server, admin, start))

    config = written(relay_fake)
    assert (config["check_interval"], config["failures"]) == (2, 4)
    assert relay_fake.docker_calls.count("build") == 1, "no reinstall"


def test_a_joining_member_updates_the_relay_at_once(  # noqa: PLR0913, PLR0917
    group: ServerGroup,
    relay_server: Server,
    relay_fake: FakeRelayServer,
    join: Callable[[], Operation],
    admin: User,
    start: Start,
) -> None:
    group.relay = relay_server
    group.save(update_fields=["relay"])
    succeeded(run(INSTALL_KIND, relay_server, admin, start))

    succeeded(join())

    succeeded(Operation.objects.filter(kind=UPDATE_KIND).latest("pk"))
    (written_group,) = written(relay_fake)["groups"]
    assert [backend["host"] for backend in written_group["backends"]] == [LEADER_HOST, JOINER_HOST]


def test_a_member_name_on_a_new_address_updates_the_relay(
    relayed: ServerGroup, relay_server: Server, admin: User, start: Start, monkeypatch: MonkeyPatch
) -> None:
    succeeded(run(INSTALL_KIND, relay_server, admin, start))
    moved = {LEADER_HOST: "198.51.100.10"}
    monkeypatch.setattr(config, "ipv4_addresses", lambda name: [moved.get(name, name)])

    sync_relays()

    assert Operation.objects.filter(kind=UPDATE_KIND).exists()


def test_sync_keeps_why_it_skips_a_relay(
    relayed: ServerGroup, relay_server: Server, admin: User, start: Start, monkeypatch: MonkeyPatch
) -> None:
    succeeded(run(INSTALL_KIND, relay_server, admin, start))

    def lost(name: str) -> list[str]:
        raise OSError(f"{name} is not known")

    monkeypatch.setattr(config, "ipv4_addresses", lost)
    sync_relays()
    relay = Relay.objects.get(server=relay_server)
    assert relay.sync_skipped == SyncSkip.HOST_NOT_RESOLVED
    assert LEADER_HOST in relay.sync_skipped_detail
    assert not Operation.objects.filter(kind=UPDATE_KIND).exists()

    monkeypatch.undo()
    sync_relays()
    relay.refresh_from_db()
    assert relay.sync_skipped == SyncSkip.NONE


def test_sync_names_a_member_on_the_relay_server(
    relayed: ServerGroup, relay_server: Server, admin: User, start: Start
) -> None:
    succeeded(run(INSTALL_KIND, relay_server, admin, start))
    joiner = GroupMember.objects.get(leading=False).instance
    joiner.server = relay_server
    joiner.save(update_fields=["server"])

    sync_relays()

    relay = Relay.objects.get(server=relay_server)
    assert relay.sync_skipped == SyncSkip.MEMBER_ON_RELAY
    assert relayed.name in relay.sync_skipped_detail


@pytest.mark.usefixtures("relayed")
def test_a_busy_member_falls_behind_without_breaking_the_install(
    relay_server: Server, joined: GroupMember, admin: User, start: Start, monkeypatch: MonkeyPatch
) -> None:
    def busy(*_args: object) -> None:
        raise ServerLockError(ErrorCode.SERVER_BUSY, "exit 75")

    monkeypatch.setattr(PeerManager, "enable", busy)

    operation = run(INSTALL_KIND, relay_server, admin, start)

    succeeded(operation)
    joined.refresh_from_db()
    assert joined.lagging and not leading().lagging
    logs = "".join(step.log for step in operation.steps.all())
    assert ServerLockError(ErrorCode.SERVER_BUSY).message in logs


def leading() -> GroupMember:
    return GroupMember.objects.get(leading=True)


def nobody_lags() -> bool:
    return not GroupMember.objects.exclude(lagging_since=None).exists()


def busy_first(monkeypatch: MonkeyPatch, method: str) -> None:
    """The first call of the PeerManager method, made on the leading server, finds it busy."""
    original = getattr(PeerManager, method)
    calls: list[object] = []

    def busy(self: PeerManager, *args: object, **kwargs: object) -> object:
        calls.append(self)
        if len(calls) == 1:
            raise ServerLockError(ErrorCode.SERVER_BUSY, "exit 75")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PeerManager, method, busy)


@pytest.mark.usefixtures("relayed")
def test_a_busy_leader_fails_the_install_and_does_not_lag(
    relay_server: Server, admin: User, start: Start, monkeypatch: MonkeyPatch
) -> None:
    busy_first(monkeypatch, "add")

    operation = run(INSTALL_KIND, relay_server, admin, start)

    assert operation.status == OperationStatus.FAILED
    assert operation.error_code == ErrorCode.SERVER_BUSY
    assert nobody_lags()
    assert not ServicePeer.objects.exists()


@pytest.mark.usefixtures("relayed")
def test_a_busy_leader_fails_the_update_and_the_relay_keeps_its_members(
    relay_server: Server,
    relay_fake: FakeRelayServer,
    admin: User,
    start: Start,
    monkeypatch: MonkeyPatch,
) -> None:
    succeeded(run(INSTALL_KIND, relay_server, admin, start))
    before = written(relay_fake)
    busy_first(monkeypatch, "enable")

    operation = run(UPDATE_KIND, relay_server, admin, start)

    assert operation.status == OperationStatus.FAILED
    assert nobody_lags()
    assert written(relay_fake) == before


@pytest.mark.usefixtures("relayed")
@pytest.mark.parametrize("kind", [INSTALL_KIND, UPDATE_KIND])
def test_a_leader_that_does_not_answer_leaves_the_service_peer_for_later(
    kind: str, relay_server: Server, down: set[str], admin: User, start: Start
) -> None:
    if kind == UPDATE_KIND:
        succeeded(run(INSTALL_KIND, relay_server, admin, start))
        ServicePeer.objects.all().delete()
    down.add(LEADER_HOST)

    operation = run(kind, relay_server, admin, start)

    succeeded(operation)
    assert nobody_lags()
    logs = "".join(step.log for step in operation.steps.all())
    assert RelayError(RelayErrorCode.LEADER_UNREACHABLE, "Europe").message in logs


@pytest.mark.usefixtures("relayed")
def test_a_leader_that_does_not_answer_stays_on_the_relay(
    relay_server: Server, relay_fake: FakeRelayServer, down: set[str], admin: User, start: Start
) -> None:
    succeeded(run(INSTALL_KIND, relay_server, admin, start))
    down.add(LEADER_HOST)

    succeeded(run(UPDATE_KIND, relay_server, admin, start))

    assert nobody_lags()
    (group,) = written(relay_fake)["groups"]
    assert [backend["host"] for backend in group["backends"]] == [LEADER_HOST, JOINER_HOST]


@pytest.mark.usefixtures("relayed")
def test_a_busy_leader_fails_the_removal_and_keeps_the_service_peer(
    relay_server: Server, admin: User, start: Start, monkeypatch: MonkeyPatch
) -> None:
    succeeded(run(INSTALL_KIND, relay_server, admin, start))
    busy_first(monkeypatch, "revoke")

    operation = run(REMOVE_KIND, relay_server, admin, start)

    assert operation.status == OperationStatus.FAILED
    assert nobody_lags()
    assert ServicePeer.objects.exists()


@pytest.mark.usefixtures("relayed")
def test_a_leader_down_at_removal_keeps_the_service_peer_on_every_member(
    relay_server: Server, servers: Fakes, down: set[str], admin: User, start: Start
) -> None:
    succeeded(run(INSTALL_KIND, relay_server, admin, start))
    public_key = ServicePeer.objects.get().public_key
    down.add(LEADER_HOST)

    succeeded(run(REMOVE_KIND, relay_server, admin, start))

    assert nobody_lags()
    assert public_key in peers_of(servers[JOINER_HOST])
    assert ServicePeer.objects.exists()


def heard(server: Server, *, ago: timedelta) -> None:
    """The monitor last heard the server `ago`."""
    at = timezone.now() - ago
    sample = ServerSample.objects.create(server=server, at=at)
    Server.objects.filter(pk=server.pk).update(latest_sample=sample, last_seen_at=at)


@pytest.mark.usefixtures("relayed")
def test_sync_places_the_probe_once_the_leader_answers_again(  # noqa: PLR0913, PLR0917
    relay_server: Server,
    servers: Fakes,
    down: set[str],
    admin: User,
    start: Start,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    down.add(LEADER_HOST)
    succeeded(run(INSTALL_KIND, relay_server, admin, start))
    assert not ServicePeer.objects.exists()
    leader_server = leading().instance.server

    heard(leader_server, ago=timedelta(hours=1))
    with django_capture_on_commit_callbacks(execute=True):
        sync_relays()
    assert not Operation.objects.filter(kind=UPDATE_KIND).exists(), "the leader is still down"

    down.discard(LEADER_HOST)
    heard(leader_server, ago=timedelta(0))
    with django_capture_on_commit_callbacks(execute=True):
        sync_relays()

    succeeded(Operation.objects.get(kind=UPDATE_KIND))
    probe = ServicePeer.objects.get(role=PeerRole.PROBE)
    assert all(probe.public_key in peers_of(fake) for fake in servers.values())


@pytest.mark.usefixtures("relayed")
def test_sync_with_every_service_peer_connects_nowhere(
    relay_server: Server,
    admin: User,
    start: Start,
    monkeypatch: MonkeyPatch,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    succeeded(run(INSTALL_KIND, relay_server, admin, start))
    heard(leading().instance.server, ago=timedelta(0))

    def no_connection(*args: object, **kwargs: object) -> None:
        raise AssertionError("the sync must not connect")

    monkeypatch.setattr(deployment, "connect", no_connection)
    monkeypatch.setattr(replication, "connect", no_connection)
    with django_capture_on_commit_callbacks(execute=True):
        sync_relays()

    assert not Operation.objects.filter(kind=UPDATE_KIND).exists()


def test_sync_places_the_uplink_a_split_group_lacks(
    relayed: ServerGroup,
    relay_server: Server,
    admin: User,
    start: Start,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    succeeded(run(INSTALL_KIND, relay_server, admin, start))
    ServerGroup.objects.filter(pk=relayed.pk).update(routing=GroupRouting.SPLIT)
    relay = Relay.objects.get(server=relay_server)
    plan = config.RelayPlan.of(relay_server)
    Relay.objects.filter(pk=relay.pk).update(synced=plan.digest(plan.config()))
    assert not ServicePeer.objects.filter(role=PeerRole.UPLINK).exists()

    with django_capture_on_commit_callbacks(execute=True):
        sync_relays()

    assert Operation.objects.filter(kind=UPDATE_KIND).exists()
