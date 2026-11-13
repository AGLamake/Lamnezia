from ipaddress import IPv4Interface, IPv4Network

import pytest

from amnezia_core.awg import AwgServerConfig
from amnezia_core.awg_export import AwgClientProfile, AwgServerSettings
from amnezia_core.awg_install import AwgInstallSettings
from amnezia_core.clients_table import ClientsTable
from amnezia_core.keys import KeyPair
from apps.operations.models import Operation, StepStatus
from apps.operations.services.context import OperationContext
from apps.operations.tests.factories import OperationFactory
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.services.awg_installer import AwgInstaller
from apps.protocols.tests.fake_awg import START_SCRIPT, FakeAwgServer

pytestmark = pytest.mark.django_db

CONFIG = "/opt/amnezia/awg/awg0.conf"
CLIENTS_TABLE = "/opt/amnezia/awg/clientsTable"
APP_START_SCRIPT = b"#!/bin/bash\nawg-quick up /opt/amnezia/awg/awg0.conf\n# 10.9.0.0/24\n"


def install(
    server: FakeAwgServer, settings: AwgInstallSettings | None = None
) -> tuple[AwgServerConfig, Operation]:
    operation = OperationFactory()
    ctx = OperationContext(operation)
    config = AwgInstaller(server.host(), ctx, settings or AwgInstallSettings()).run()
    return config, operation


def fail_install(server: FakeAwgServer) -> tuple[SetupError, Operation]:
    operation = OperationFactory()
    with pytest.raises(SetupError) as caught:
        AwgInstaller(server.host(), OperationContext(operation), AwgInstallSettings()).run()
    return caught.value, operation


def log_of(operation: Operation) -> str:
    return "\n".join(operation.steps.values_list("log", flat=True))


def app_installed_server(state: str = "exited") -> FakeAwgServer:
    keys = KeyPair.generate()
    config = (
        f"[Interface]\nPrivateKey = {keys.private_key}\nAddress = 10.9.0.0/24\nListenPort = 40000\n"
    )
    return FakeAwgServer(
        container_state=state,
        container_networks={"bridge", "amnezia-dns-net"},
        container_files={
            CONFIG: config.encode(),
            CLIENTS_TABLE: b'[{"clientId": "x", "userData": {"clientName": "phone"}}]',
            START_SCRIPT: APP_START_SCRIPT,
        },
    )


def test_clean_server_gets_a_running_awg_container() -> None:
    server = FakeAwgServer()

    config, operation = install(server)

    assert server.docker_calls.count("build") == 1
    assert server.docker_calls.count("run") == 1
    assert "amnezia-dns-net" in server.container_networks
    assert server.awg_up
    assert config.listen_port == 55424
    assert config.address == IPv4Interface("10.8.1.0/24")
    assert config.private_key == server.server_keys.private_key
    assert set(operation.steps.values_list("status", flat=True)) == {StepStatus.SUCCEEDED}
    assert server.server_keys.public_key in log_of(operation)


def test_container_runs_without_a_log_unless_asked() -> None:
    plain, kept = FakeAwgServer(), FakeAwgServer()
    ctx = OperationContext(OperationFactory())

    install(plain)
    AwgInstaller(kept.host(), ctx, AwgInstallSettings(), logs=True).run()

    assert plain.log_driver == "none"
    assert kept.log_driver == "json-file"


def test_installed_files_are_what_the_amnezia_app_reads() -> None:
    server = FakeAwgServer()

    config, _ = install(server, AwgInstallSettings(port=40001, subnet=IPv4Network("10.9.8.0/24")))

    assert config.awg_params["I1"].startswith("<r 2>")
    assert "ContentPaddingAddition" not in config.awg_params
    assert len(ClientsTable.parse(server.container_files[CLIENTS_TABLE].decode())) == 0
    start = server.container_files[START_SCRIPT].decode()
    assert "-s 10.9.8.0/24 -o eth0 -j MASQUERADE" in start
    assert "$" + "AWG_SUBNET_IP" not in start
    settings = AwgServerSettings.from_config(config)
    profile = AwgClientProfile(
        host="203.0.113.7",
        server=settings,
        server_public_key=server.server_keys.public_key,
        keys=KeyPair.generate(),
        preshared_key="",
        client_ip=IPv4Interface("10.9.8.2/32").ip,
    )
    assert "Endpoint = 203.0.113.7:40001" in profile.native_config()


def test_second_run_keeps_keys_and_does_not_rebuild() -> None:
    server = FakeAwgServer()
    install(server)
    files = dict(server.container_files)
    server.docker_calls.clear()

    _, operation = install(server)

    assert server.container_files == files
    assert "build" not in server.docker_calls
    assert "run" not in server.docker_calls
    assert "The VPN interface is already up." in log_of(operation)


def test_container_from_the_app_is_started_but_never_overwritten() -> None:
    server = app_installed_server()
    files = dict(server.container_files)

    config, operation = install(server)

    assert server.container_files == files
    assert server.docker_calls.count("start") == 1
    assert "build" not in server.docker_calls
    assert config.listen_port == 40000
    assert "already exists and is kept" in log_of(operation)


def test_retry_after_a_half_install_finishes_it() -> None:
    server = FakeAwgServer(
        container_state="running",
        container_networks={"bridge"},
        container_files={START_SCRIPT: b"#!/bin/bash\ntail -f /dev/null\n"},
    )

    config, _ = install(server)

    assert config.listen_port == 55424
    assert "amnezia-dns-net" in server.container_networks
    assert b"awg-quick up" in server.container_files[START_SCRIPT]
    assert server.awg_up


def test_busy_port_stops_before_the_build() -> None:
    server = FakeAwgServer(port_users="coturn 912 root 5u IPv4 UDP *:55424\n")

    error, operation = fail_install(server)

    assert error.code == SetupErrorCode.PORT_BUSY
    assert "build" not in server.docker_calls
    assert "coturn" in log_of(operation)


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        (
            b"toomanyrequests: You have reached your pull rate limit.",
            SetupErrorCode.DOCKER_PULL_LIMIT,
        ),
        (b"ERROR: failed to solve: process did not complete", SetupErrorCode.IMAGE_BUILD_FAILED),
    ],
)
def test_build_failure_is_explained(stderr: bytes, code: SetupErrorCode) -> None:
    error, operation = fail_install(FakeAwgServer(build_error=stderr))

    assert error.code == code
    assert stderr.decode() in log_of(operation)


def test_failed_docker_run_removes_the_container_for_the_retry() -> None:
    server = FakeAwgServer(
        run_error=b"Error starting userland proxy: listen udp 0.0.0.0:55424: "
        b"bind: address already in use"
    )

    error, _ = fail_install(server)

    assert error.code == SetupErrorCode.PORT_BUSY
    assert server.container_state == ""


def test_old_kernel_is_refused_before_any_change() -> None:
    server = FakeAwgServer(kernel="4.9.0-19-amd64")

    error, _ = fail_install(server)

    assert error.code == SetupErrorCode.KERNEL_TOO_OLD
    assert server.locks == 0


def test_awg_that_does_not_come_up_fails_the_last_step() -> None:
    error, operation = fail_install(FakeAwgServer(awg_starts=False))

    assert error.code == SetupErrorCode.AWG_NOT_RUNNING
    assert operation.steps.get(status=StepStatus.FAILED).title == "Start AmneziaWG 2.0"
