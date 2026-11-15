import pytest
from django.test import Client
from django.urls import reverse

from amnezia_core.awg import AWG_CLIENTS_TABLE_PATH
from amnezia_core.awg_group import AwgGroupIdentity, standalone_files
from amnezia_core.clients_table import ClientsTable
from apps.accounts.models import User
from apps.backups.models import ProtocolBackup, ProtocolSnapshot
from apps.backups.tests.fake_containers import TarAwgServer
from apps.groups.errors import GroupError, GroupErrorCode
from apps.groups.models import GroupMember, MemberState, ServerGroup
from apps.groups.services.membership import INSTALL_KIND, servers_without_awg, start_install
from apps.groups.tests.conftest import (
    LEADER_FILES,
    LEADER_HOST,
    PORT,
    Fakes,
    Run,
    fake_server,
    server_files,
    succeeded,
)
from apps.operations.models import Operation, OperationStatus
from apps.protocols.models import InstanceOrigin, ProtocolInstance
from apps.protocols.tests.awg_case import APP_CONFIG, Start
from apps.relays.models import Relay
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db

BARE_HOST = "203.0.113.30"


@pytest.fixture
def bare_awg(servers: Fakes) -> TarAwgServer:
    """A server with Docker and nothing of Amnezia on it."""
    awg = TarAwgServer()
    servers[BARE_HOST] = awg
    return awg


@pytest.fixture
def bare() -> Server:
    return ServerFactory(host=BARE_HOST)


@pytest.fixture
def install(
    bare_awg: TarAwgServer, group: ServerGroup, bare: Server, admin: User, start: Start
) -> Run:
    """Run the install of the group's AmneziaWG on the bare server."""
    return lambda: start(lambda: start_install(group, bare, user=admin))


def is_clone(servers: Fakes) -> bool:
    leader_files = server_files(servers[LEADER_HOST])
    return AwgGroupIdentity(leader_files).compare(server_files(servers[BARE_HOST])) == []


def test_server_without_awg_becomes_a_clone_of_the_leader(
    servers: Fakes, leader: ProtocolInstance, bare: Server, install: Run
) -> None:
    succeeded(install())

    assert is_clone(servers)
    assert servers[BARE_HOST].awg_up
    instance = ProtocolInstance.objects.get(server=bare, container="amnezia-awg2")
    assert instance.origin == InstanceOrigin.INSTALLED
    assert instance.private_key == leader.private_key
    assert instance.public_key == leader.public_key
    assert instance.preshared_key == leader.preshared_key
    assert instance.params == leader.params
    assert (instance.port, instance.subnet) == (leader.port, leader.subnet)
    member = GroupMember.objects.get(instance=instance)
    assert member.state == MemberState.MEMBER
    assert member.joined_at is not None
    assert not member.leading
    assert not ProtocolSnapshot.objects.exists()
    assert ProtocolBackup.objects.filter(instance=instance).exists()
    bare.refresh_from_db()
    assert {"name": "amnezia-awg2", "port": PORT, "transport": "udp"} in bare.containers


def test_taken_port_refuses_the_join_before_the_install(
    servers: Fakes, bare: Server, install: Run
) -> None:
    servers[BARE_HOST].port_users = f"nginx 1234 root 6u IPv4 UDP *:{PORT}\n"

    operation = install()

    assert operation.status == OperationStatus.FAILED
    assert operation.error_code == "port_busy"
    assert not {"build", "run"} & set(servers[BARE_HOST].docker_calls)
    assert servers[BARE_HOST].container_state == ""
    assert not ProtocolInstance.objects.filter(server=bare).exists()


def test_port_the_panel_knows_taken_is_refused_at_once(
    bare_awg: TarAwgServer, group: ServerGroup, bare: Server, admin: User
) -> None:
    bare.record_containers([{"name": "amnezia-xray", "port": PORT, "transport": "udp"}])

    with pytest.raises(GroupError) as error:
        start_install(group, bare, user=admin)

    assert error.value.code == GroupErrorCode.PORT_TAKEN
    assert not Operation.objects.filter(kind=INSTALL_KIND).exists()


def test_container_with_other_keys_is_not_replaced(
    servers: Fakes, bare: Server, install: Run
) -> None:
    own = fake_server(standalone_files(APP_CONFIG.address, PORT))
    servers[BARE_HOST].container_state = own.container_state
    servers[BARE_HOST].container_files = own.container_files
    before = server_files(servers[BARE_HOST])

    operation = install()

    assert operation.status == OperationStatus.FAILED
    assert operation.error_code == GroupErrorCode.AWG_ON_SERVER
    assert server_files(servers[BARE_HOST]) == before
    assert not ProtocolInstance.objects.filter(server=bare).exists()


def test_retry_brings_older_files_of_the_group_up_to_date(servers: Fakes, install: Run) -> None:
    earlier = fake_server(LEADER_FILES)
    earlier.container_files[AWG_CLIENTS_TABLE_PATH] = ClientsTable([]).serialize().encode()
    servers[BARE_HOST].container_state = earlier.container_state
    servers[BARE_HOST].container_files = earlier.container_files

    succeeded(install())

    assert is_clone(servers)


def test_servers_with_awg_or_a_relay_are_not_offered(
    bare: Server, leader: ProtocolInstance, joiner: ProtocolInstance
) -> None:
    relay = ServerFactory()
    Relay.objects.create(server=relay)
    seen = ServerFactory()
    seen.record_containers([{"name": "amnezia-awg2", "port": PORT, "transport": "udp"}])

    assert servers_without_awg() == [bare]


def test_relay_server_is_refused(group: ServerGroup, bare: Server, admin: User) -> None:
    Relay.objects.create(server=bare)

    with pytest.raises(GroupError) as error:
        start_install(group, bare, user=admin)

    assert error.value.code == GroupErrorCode.RELAY_SERVER


def test_relay_of_the_group_is_refused_before_its_relay_runs(
    group: ServerGroup, bare: Server, admin: User
) -> None:
    group.relay = bare
    group.save(update_fields=["relay"])

    with pytest.raises(GroupError) as error:
        start_install(group, bare, user=admin)

    assert error.value.code == GroupErrorCode.GROUP_RELAY


def test_server_running_awg_unknown_to_the_panel_is_refused(
    group: ServerGroup, bare: Server, admin: User
) -> None:
    bare.record_containers([{"name": "amnezia-awg2", "port": 1, "transport": "udp"}])

    with pytest.raises(GroupError) as error:
        start_install(group, bare, user=admin)

    assert error.value.code == GroupErrorCode.AWG_ON_SERVER


def test_second_install_while_one_runs_is_refused(
    group: ServerGroup, bare: Server, admin: User
) -> None:
    start_install(group, bare, user=admin)

    with pytest.raises(GroupError) as error:
        start_install(group, bare, user=admin)

    assert error.value.code == GroupErrorCode.JOIN_RUNNING


def test_card_offers_the_install_and_starts_it(
    client: Client, admin: User, group: ServerGroup, bare: Server
) -> None:
    client.force_login(admin)
    detail = reverse("groups:detail", args=[group.pk])
    install = reverse("groups:install", args=[group.pk, bare.pk])
    assert install in client.get(detail).content.decode()

    client.post(install)

    operation = Operation.objects.get(kind=INSTALL_KIND)
    assert operation.params == {"group_id": group.pk, "server_id": bare.pk}
