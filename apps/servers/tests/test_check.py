from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from apps.operations.models import Operation, OperationStatus, StepStatus
from apps.operations.services.context import OperationContext
from apps.operations.services.runner import OperationRunner
from apps.operations.tests.factories import OperationFactory
from apps.protocols.tests.fakes import DEBIAN_12
from apps.servers.models import CheckWarning, Server, ServerStatus
from apps.servers.services.check import CHECK_KIND, ServerCheck
from apps.servers.services.history import latest_operation
from apps.servers.tests.factories import ServerFactory
from apps.ssh.errors import ErrorCode, SshConnectionError, SshPrivilegeError
from apps.ssh.services import CommandResult, RemoteHost
from apps.ssh.tests.fakes import TIMEOUTS, FakeRunner, result

pytestmark = pytest.mark.django_db

GB = 1024**3
CENTOS = b'PRETTY_NAME="CentOS Stream 9"\nID="centos"\nID_LIKE="rhel fedora"\n'


def fake_server(
    os_release: bytes = DEBIAN_12,
    docker: CommandResult | None = None,
    free_kb: bytes = b"20971520\n",
    answers: dict[str, CommandResult] | None = None,
) -> FakeRunner:
    return FakeRunner(
        {
            "/etc/os-release": result(stdout=os_release),
            "uname -sr": result(stdout=b"Linux 6.1.0-28-amd64\n"),
            "docker --version": docker or result(stdout=b"Docker version 26.1.5\n"),
            "df -Pk": result(stdout=free_kb),
            **(answers or {}),
        }
    )


def run_check(runner: FakeRunner) -> tuple[Operation, list[str]]:
    operation = OperationFactory()
    facts = ServerCheck(RemoteHost(runner, TIMEOUTS), OperationContext(operation), 2 * GB).run()
    return operation, [str(w) for w in facts.warnings]


def test_a_ready_debian_server_passes_without_warnings() -> None:
    operation, warnings = run_check(fake_server())

    assert warnings == []
    logs = "".join(operation.steps.values_list("log", flat=True))
    assert "Debian GNU/Linux 12 (bookworm), Linux 6.1.0-28-amd64" in logs
    assert "Docker version 26.1.5" in logs
    assert "20.0 GB free" in logs


def test_findings_that_do_not_block_the_server_become_warnings() -> None:
    runner = fake_server(
        CENTOS, docker=result(127, stderr=b"sh: docker: not found"), free_kb=b"1024\n"
    )

    _, warnings = run_check(runner)

    assert warnings == [CheckWarning.UNSUPPORTED_OS, CheckWarning.NO_DOCKER, CheckWarning.LOW_DISK]


def test_missing_root_access_fails_the_check() -> None:
    runner = fake_server(
        answers={
            "id -u": result(stdout=b"1000\n"),
            "sudo -n true": result(1, stderr=b"sudo: a password is required\n"),
            "id -nG": result(stdout=b"deploy sudo\n"),
        }
    )

    with pytest.raises(SshPrivilegeError) as raised:
        run_check(runner)

    assert raised.value.code == ErrorCode.SUDO_PASSWORD_REQUIRED


@contextmanager
def connected(runner: FakeRunner) -> Iterator[RemoteHost]:
    yield RemoteHost(runner, TIMEOUTS)


def check_operation(server: Server) -> Operation:
    return OperationFactory(kind=CHECK_KIND, params={"server_id": server.pk})


def test_check_operation_saves_what_it_found(monkeypatch: pytest.MonkeyPatch) -> None:
    server = ServerFactory()
    monkeypatch.setattr("apps.servers.operations.connect", lambda _: connected(fake_server()))
    operation = check_operation(server)

    OperationRunner().run(operation.pk)

    server.refresh_from_db()
    operation.refresh_from_db()
    assert operation.status == OperationStatus.SUCCEEDED
    assert server.status == ServerStatus.READY
    assert (server.os_name, server.docker_version) == (
        "Debian GNU/Linux 12 (bookworm)",
        "Docker version 26.1.5",
    )
    assert server.disk_free_bytes == 20 * GB
    assert server.checked_at is not None


def test_failed_login_explains_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    server = ServerFactory()

    def refuse(_: Server) -> Iterator[RemoteHost]:
        raise SshConnectionError(ErrorCode.AUTH_FAILED, "Authentication failed.")

    monkeypatch.setattr("apps.servers.operations.connect", refuse)
    operation = check_operation(server)

    OperationRunner().run(operation.pk)

    server.refresh_from_db()
    operation.refresh_from_db()
    assert server.status == ServerStatus.FAILED
    assert operation.error_code == ErrorCode.AUTH_FAILED
    assert operation.error_message == "The server rejected the login."
    assert operation.steps.get().status == StepStatus.FAILED


def test_latest_operation_is_the_newest_one_of_this_server() -> None:
    server, other = ServerFactory(), ServerFactory()
    check_operation(server)
    newest = check_operation(server)
    check_operation(other)

    assert latest_operation(server) == newest
