"""The relay container on a fake server: ports are checked before anything is started."""

import pytest

from apps.operations.services.context import OperationContext
from apps.operations.tests.factories import OperationFactory
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.relays.errors import RelayError, RelayErrorCode
from apps.relays.services.container import FOLDER, NAME, RelayContainer, agent_source
from apps.relays.tests.fake_relay import FakeRelayServer

pytestmark = pytest.mark.django_db

PORT = 55424
CONFIG = b'{"version": 1}'


@pytest.fixture
def server() -> FakeRelayServer:
    return FakeRelayServer()


def relay(server: FakeRelayServer) -> RelayContainer:
    return RelayContainer(server.host(), OperationContext(OperationFactory()))


def check(server: FakeRelayServer, *ports: int) -> None:
    """The deployment checks the ports inside its own step."""
    ctx = OperationContext(OperationFactory())
    with ctx.step("check"):
        RelayContainer(server.host(), ctx).check_ports(set(ports))


def installed(server: FakeRelayServer, *ports: int) -> RelayContainer:
    container = relay(server)
    container.install(set(ports))
    container.apply(set(ports), CONFIG)
    return container


def test_a_port_of_the_panel_web_server_is_refused(server: FakeRelayServer) -> None:
    with pytest.raises(RelayError) as error:
        check(server, PORT, 443)

    assert error.value.code == RelayErrorCode.PORT_RESERVED
    assert "build" not in server.docker_calls


def test_a_port_another_container_publishes_is_refused(server: FakeRelayServer) -> None:
    server.others = [f"amnezia-awg2 0.0.0.0:{PORT}->{PORT}/udp, :::{PORT}->{PORT}/udp"]

    with pytest.raises(RelayError) as error:
        check(server, PORT)

    assert error.value.code == RelayErrorCode.PORT_BUSY


def test_a_port_another_program_listens_on_is_refused(server: FakeRelayServer) -> None:
    server.port_users = f"wireguard 812 root 3u IPv4 UDP *:{PORT}\n"

    with pytest.raises(SetupError) as error:
        check(server, PORT)

    assert error.value.code == SetupErrorCode.PORT_BUSY


def test_install_publishes_the_ports_in_its_own_network(server: FakeRelayServer) -> None:
    installed(server, PORT, 51820)

    arguments = server.run_arguments
    assert server.ports == {PORT, 51820}
    assert "--network" not in arguments and "host" not in arguments
    assert {"--cap-add=NET_ADMIN", "/dev/net/tun", "net.ipv4.ip_forward=1"} <= set(arguments)
    assert server.files[f"{FOLDER}/config.json"] == CONFIG
    assert server.files[f"{FOLDER}/agent.py"] == agent_source()
    assert "restart" not in server.docker_calls, "a new container starts the agent by itself"


def test_ports_of_the_relay_itself_pass_the_check(server: FakeRelayServer) -> None:
    installed(server, PORT)
    server.port_users = f"docker-pr 812 root 3u IPv4 UDP *:{PORT}\n"

    check(server, PORT)


def test_an_update_with_the_same_ports_keeps_the_container(server: FakeRelayServer) -> None:
    container = installed(server, PORT)
    server.docker_calls.clear()

    container.apply({PORT}, b'{"version": 1, "groups": []}')

    assert "run" not in server.docker_calls and "restart" not in server.docker_calls
    assert server.files[f"{FOLDER}/config.json"] == b'{"version": 1, "groups": []}'


def test_a_new_port_starts_the_container_again(server: FakeRelayServer) -> None:
    container = installed(server, PORT)

    container.apply({PORT, 51820}, CONFIG)

    assert server.ports == {PORT, 51820}
    assert server.files[f"{FOLDER}/agent.py"] == agent_source()


def test_a_changed_agent_is_restarted(server: FakeRelayServer) -> None:
    container = installed(server, PORT)
    server.files[f"{FOLDER}/agent.py"] = b"old agent"

    container.apply({PORT}, CONFIG)

    assert server.docker_calls[-1] == "restart"


def test_an_update_needs_the_container(server: FakeRelayServer) -> None:
    with pytest.raises(RelayError) as error:
        relay(server).apply({PORT}, CONFIG)

    assert error.value.code == RelayErrorCode.NOT_INSTALLED


def test_remove_drops_the_container_and_the_image(server: FakeRelayServer) -> None:
    container = installed(server, PORT)

    container.remove()

    assert not server.state and not server.has_image
    assert NAME not in "".join(server.others)
