import pytest

from apps.operations.models import Operation, StepStatus
from apps.operations.services.context import OperationContext
from apps.operations.tests.factories import OperationFactory
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.services.host_scripts import network_tuning_commands, tolerant
from apps.protocols.services.host_setup import HostSetup, OsRelease
from apps.protocols.tests.fakes import UBUNTU_24, FakeDebianServer

pytestmark = pytest.mark.django_db

ALL_COMMANDS = {"docker", "fuser", "lsof"}


def set_up(server: FakeDebianServer) -> Operation:
    operation = OperationFactory()
    HostSetup(server.host(), OperationContext(operation)).run()
    return operation


def fail_set_up(server: FakeDebianServer) -> tuple[SetupError, Operation]:
    operation = OperationFactory()
    with pytest.raises(SetupError) as caught:
        HostSetup(server.host(), OperationContext(operation)).run()
    return caught.value, operation


def logs(operation: Operation) -> str:
    return "\n".join(operation.steps.values_list("log", flat=True))


def test_fresh_server_gets_docker_network_and_tuning() -> None:
    server = FakeDebianServer()

    operation = set_up(server)

    assert server.installed == ["docker.io", "psmisc", "lsof"]
    assert server.changes == ["network", "tuning"]
    assert server.locks == 1
    assert list(operation.steps.values_list("status", flat=True)) == [StepStatus.SUCCEEDED] * 6
    assert "Debian GNU/Linux 12 (bookworm), Linux 6.1.0-28-amd64" in logs(operation)
    assert "Docker version 26.1.5" in logs(operation)


def test_second_run_changes_nothing_but_reapplies_idempotent_tuning() -> None:
    server = FakeDebianServer()
    set_up(server)
    server.installed.clear()
    server.changes.clear()

    operation = set_up(server)

    assert server.installed == []
    assert server.changes == ["tuning"]
    assert "Everything is already installed." in logs(operation)
    assert "The network amnezia-dns-net already exists." in logs(operation)


def test_only_missing_packages_are_installed() -> None:
    server = FakeDebianServer(commands={"docker", "lsof"}, apparmor=True)

    set_up(server)

    assert server.installed == ["psmisc", "apparmor"]


def test_ubuntu_is_supported() -> None:
    set_up(FakeDebianServer(os_release=UBUNTU_24, commands=set(ALL_COMMANDS)))


def test_other_systems_are_refused_before_any_change() -> None:
    server = FakeDebianServer(os_release=b'PRETTY_NAME="Fedora Linux 41"\nID=fedora\n')

    error, operation = fail_set_up(server)

    assert error.code == SetupErrorCode.UNSUPPORTED_OS
    assert server.locks == 0
    assert operation.steps.get().status == StepStatus.FAILED


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        (
            b"E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 912",
            SetupErrorCode.PACKAGE_MANAGER_BUSY,
        ),
        (b"E: Unable to locate package docker.io", SetupErrorCode.PACKAGE_INSTALL_FAILED),
    ],
)
def test_apt_failure_is_explained_and_logged(stderr: bytes, code: SetupErrorCode) -> None:
    error, operation = fail_set_up(FakeDebianServer(apt_error=stderr))

    assert error.code == code
    failed = operation.steps.get(status=StepStatus.FAILED)
    assert stderr.decode() in failed.log


def test_docker_that_does_not_start_fails_the_step() -> None:
    error, operation = fail_set_up(FakeDebianServer(docker_starts=False))

    assert error.code == SetupErrorCode.DOCKER_NOT_RUNNING
    assert operation.steps.get(status=StepStatus.FAILED).title == "Start Docker"


def test_settings_the_kernel_lacks_are_logged_not_failed() -> None:
    operation = set_up(FakeDebianServer())

    assert "skipped: tcp_tw_recycle" in logs(operation)


def test_tuning_commands_check_before_adding_rules() -> None:
    commands = network_tuning_commands()

    rules = [command for command in commands if command.startswith("iptables")]
    assert rules and all(" -C " in rule and "|| iptables -A " in rule for rule in rules)
    assert "sysctl -w net.ipv4.ip_forward=1" in commands
    assert tolerant(["false"]) == "{ false; } >/dev/null 2>&1 || echo false\n"


def test_os_release_parsing() -> None:
    release = OsRelease.parse(
        '# comment\nNAME="Linux Mint"\nID=linuxmint\nID_LIKE="ubuntu debian"\n'
    )

    assert release.is_supported
    assert release.pretty_name == "linuxmint"
    assert not OsRelease.parse("").is_supported
