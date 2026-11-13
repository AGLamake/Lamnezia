import shlex

import pytest

from apps.ssh.errors import (
    DockerError,
    ErrorCode,
    RemoteFileNotFoundError,
    SshCommandError,
    SshPrivilegeError,
)
from apps.ssh.services import RemoteHost
from apps.ssh.tests.fakes import TIMEOUTS, FakeRunner, result


def host_for(runner: FakeRunner) -> RemoteHost:
    return RemoteHost(runner, TIMEOUTS)


def test_root_runs_scripts_without_sudo() -> None:
    runner = FakeRunner()

    host_for(runner).run("echo ok")

    assert runner.last_command == "sh -c 'echo ok'"


def test_sudo_user_runs_scripts_through_passwordless_sudo() -> None:
    runner = FakeRunner({"id -u": result(stdout=b"1000\n")})

    host_for(runner).run("echo ok")

    assert runner.last_command == "sudo -n sh -c 'echo ok'"


def test_privileges_are_checked_once() -> None:
    runner = FakeRunner()
    host = host_for(runner)

    host.run("true")
    host.run("true")

    assert [command for command, _ in runner.calls].count("id -u") == 1


@pytest.mark.parametrize(
    ("sudo_stderr", "groups", "code"),
    [
        (b"bash: sudo: command not found\n", b"deploy\n", ErrorCode.SUDO_NOT_INSTALLED),
        (b"sudo: a password is required\n", b"deploy sudo\n", ErrorCode.SUDO_PASSWORD_REQUIRED),
        (b"sudo: a password is required\n", b"deploy\n", ErrorCode.USER_NOT_IN_SUDOERS),
        (b"deploy is not in the sudoers file\n", b"deploy\n", ErrorCode.USER_NOT_IN_SUDOERS),
    ],
)
def test_privilege_errors(sudo_stderr: bytes, groups: bytes, code: ErrorCode) -> None:
    runner = FakeRunner(
        {
            "id -u": result(stdout=b"1000\n"),
            "sudo -n true": result(1, stderr=sudo_stderr),
            "id -nG": result(stdout=groups),
        }
    )

    with pytest.raises(SshPrivilegeError) as error:
        host_for(runner).check_privileges()

    assert error.value.code == code


def test_failed_script_raises_with_stderr_in_detail() -> None:
    runner = FakeRunner({"broken": result(2, stderr=b"boom")})

    with pytest.raises(SshCommandError) as error:
        host_for(runner).run("broken")

    assert error.value.code == ErrorCode.COMMAND_FAILED
    assert "boom" in error.value.detail


def test_read_file_returns_bytes() -> None:
    runner = FakeRunner({"cat --": result(stdout=b"[Interface]\n")})

    assert host_for(runner).read_file("/opt/amnezia/awg/awg0.conf") == b"[Interface]\n"


def test_read_missing_file() -> None:
    runner = FakeRunner({"cat --": result(66)})

    with pytest.raises(RemoteFileNotFoundError) as error:
        host_for(runner).read_file("/opt/amnezia/awg/clientsTable")

    assert error.value.code == ErrorCode.REMOTE_FILE_NOT_FOUND


def test_write_file_atomic_sends_data_by_stdin_and_renames() -> None:
    runner = FakeRunner()

    host_for(runner).write_file_atomic("/opt/amnezia/awg/awg0.conf", b"PrivateKey = secret")

    command, stdin = runner.calls[-1]
    assert stdin == b"PrivateKey = secret"
    assert "secret" not in command
    assert "mktemp" in command
    assert 'mv -f -- "$tmp" "$target"' in command


def test_paths_are_quoted() -> None:
    runner = FakeRunner()

    host_for(runner).write_file_atomic("/tmp/a b; rm -rf /", b"")

    script = shlex.split(runner.last_command)[-1]
    assert "target='/tmp/a b; rm -rf /'" in script


def test_container_scripts_run_through_docker_exec() -> None:
    runner = FakeRunner()

    host_for(runner).container("amnezia-awg2").run("awg show all dump")

    command = runner.last_command
    assert command.startswith("sh -c ")
    assert "docker exec -i amnezia-awg2 sh -c" in command
    assert "awg show all dump" in command


def test_container_file_read() -> None:
    runner = FakeRunner({"docker exec": result(stdout=b"[]")})

    assert host_for(runner).container("amnezia-awg2").read_file("/opt/amnezia/awg/clientsTable")


@pytest.mark.parametrize(
    ("exit_code", "stderr", "code"),
    [
        (
            1,
            b"Error response from daemon: No such container: amnezia-awg2",
            ErrorCode.CONTAINER_NOT_FOUND,
        ),
        (
            125,
            b"Error response from daemon: No such container: amnezia-awg2",
            ErrorCode.CONTAINER_NOT_FOUND,
        ),
        (
            1,
            b"Error response from daemon: container abc is not running",
            ErrorCode.CONTAINER_NOT_RUNNING,
        ),
        (127, b"sh: 1: docker: not found", ErrorCode.DOCKER_NOT_INSTALLED),
    ],
)
def test_docker_errors(exit_code: int, stderr: bytes, code: ErrorCode) -> None:
    runner = FakeRunner({"docker exec": result(exit_code, stderr=stderr)})

    with pytest.raises(DockerError) as error:
        host_for(runner).container("amnezia-awg2").run("true")

    assert error.value.code == code


def test_script_output_mentioning_containers_is_not_a_docker_error() -> None:
    runner = FakeRunner({"docker exec": result(1, stderr=b"No such container: in my log")})

    with pytest.raises(SshCommandError):
        host_for(runner).container("amnezia-awg2").run("cat log")


def test_missing_command_inside_container_is_a_command_failure() -> None:
    runner = FakeRunner({"docker exec": result(127, stderr=b"sh: awg: not found")})

    with pytest.raises(SshCommandError) as error:
        host_for(runner).container("amnezia-awg2").run("awg show")

    assert error.value.code == ErrorCode.COMMAND_FAILED


def test_docker_cli_helper() -> None:
    runner = FakeRunner({"docker ps": result(stdout=b"amnezia-awg2\n")})

    output = host_for(runner).docker("ps", "--format", "{{.Names}}")

    assert output == "amnezia-awg2\n"
    assert shlex.split(runner.last_command)[-1] == "docker ps --format '{{.Names}}'"
