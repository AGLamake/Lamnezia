import io
import socket
from collections.abc import Iterator

import paramiko
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from apps.ssh.errors import ErrorCode, SshCommandError, SshConnectionError
from apps.ssh.models import KnownHost
from apps.ssh.services import SshConnection, SshCredentials, SshTimeouts
from apps.ssh.tests.fake_sshd import PASSWORD, USERNAME, fake_sshd
from apps.ssh.tests.fakes import TIMEOUTS

pytestmark = pytest.mark.django_db


@pytest.fixture
def host_key() -> paramiko.PKey:
    return _paramiko_ed25519()


@pytest.fixture
def client_key_text() -> str:
    return _openssh_private_key()


@pytest.fixture
def port(host_key: paramiko.PKey, client_key_text: str) -> Iterator[int]:
    client_key = paramiko.Ed25519Key.from_private_key(io.StringIO(client_key_text))
    with fake_sshd(host_key, authorized_key=client_key) as port:
        yield port


def credentials(port: int, password: str = PASSWORD) -> SshCredentials:
    return SshCredentials(host="127.0.0.1", port=port, username=USERNAME, password=password)


def test_runs_command_and_returns_output(port: int) -> None:
    with SshConnection(credentials(port), timeouts=TIMEOUTS) as connection:
        result = connection.run("hello")

    assert result.ok
    assert result.output == "hello"


def test_logs_in_with_private_key(port: int, client_key_text: str) -> None:
    creds = SshCredentials(
        host="127.0.0.1", port=port, username=USERNAME, private_key=client_key_text
    )

    with SshConnection(creds, timeouts=TIMEOUTS) as connection:
        assert connection.run("hello").output == "hello"


def test_passes_stdin_to_command(port: int) -> None:
    payload = b"[Interface]\n" * 10000

    with SshConnection(credentials(port), timeouts=TIMEOUTS) as connection:
        result = connection.run("cat", stdin=payload)

    assert result.stdout == payload


def test_returns_exit_code_and_stderr(port: int) -> None:
    with SshConnection(credentials(port), timeouts=TIMEOUTS) as connection:
        result = connection.run("exit 3")

    assert result.exit_code == 3
    assert result.errors == "failed\n"


def test_command_timeout(port: int) -> None:
    with (
        SshConnection(credentials(port), timeouts=TIMEOUTS) as connection,
        pytest.raises(SshCommandError) as error,
    ):
        connection.run("sleep", timeout=0.3)

    assert error.value.code == ErrorCode.COMMAND_TIMEOUT


def test_long_running_process_ends_when_stdin_closes(port: int) -> None:
    with SshConnection(credentials(port), timeouts=TIMEOUTS) as connection:
        process = connection.start("hold")
        line = process.read_line(timeout=1)
        process.close_stdin()
        exit_code = process.wait(timeout=1)
        process.close()

    assert line == "held"
    assert exit_code == 0


def test_first_connection_saves_host_key(port: int, host_key: paramiko.PKey) -> None:
    with SshConnection(credentials(port), timeouts=TIMEOUTS):
        pass

    known = KnownHost.objects.get(host="127.0.0.1", port=port)
    assert known.fingerprint == host_key.fingerprint


def test_changed_host_key_is_refused(port: int) -> None:
    KnownHost.objects.create(
        host="127.0.0.1", port=port, key_type="ssh-ed25519", fingerprint="SHA256:other"
    )

    with pytest.raises(SshConnectionError) as error:
        SshConnection(credentials(port), timeouts=TIMEOUTS).connect()

    assert error.value.code == ErrorCode.HOST_KEY_MISMATCH


def test_wrong_password(port: int) -> None:
    with pytest.raises(SshConnectionError) as error:
        SshConnection(credentials(port, password="wrong"), timeouts=TIMEOUTS).connect()

    assert error.value.code == ErrorCode.AUTH_FAILED


def test_closed_port_is_unreachable() -> None:
    with socket.create_server(("127.0.0.1", 0)) as probe:
        free_port = probe.getsockname()[1]

    # Windows reports a refused local connection only after about two seconds.
    timeouts = SshTimeouts(connect=10, command=1, lock=1)

    with pytest.raises(SshConnectionError) as error:
        SshConnection(credentials(free_port), timeouts=timeouts).connect()

    assert error.value.code == ErrorCode.HOST_UNREACHABLE


def test_run_without_connection_reports_lost_connection() -> None:
    with pytest.raises(SshCommandError) as error:
        SshConnection(credentials(22), timeouts=TIMEOUTS).run("true")

    assert error.value.code == ErrorCode.CONNECTION_LOST


@pytest.mark.parametrize(
    ("key_text", "passphrase", "code"),
    [
        ("not a key", "", ErrorCode.PRIVATE_KEY_INVALID),
        ("encrypted", "", ErrorCode.PRIVATE_KEY_PASSPHRASE),
        ("encrypted", "wrong", ErrorCode.PRIVATE_KEY_PASSPHRASE),
    ],
)
def test_unreadable_private_key(key_text: str, passphrase: str, code: ErrorCode) -> None:
    if key_text == "encrypted":
        key_text = _openssh_private_key(b"right")
    creds = SshCredentials(
        host="127.0.0.1", port=22, username="u", private_key=key_text, passphrase=passphrase
    )

    with pytest.raises(SshConnectionError) as error:
        SshConnection(creds, timeouts=TIMEOUTS).connect()

    assert error.value.code == code


def test_credentials_repr_hides_secrets() -> None:
    creds = SshCredentials(host="h", port=22, username="u", password="pw", private_key="KEY")

    assert "pw" not in repr(creds)
    assert "KEY" not in repr(creds)


def _openssh_private_key(passphrase: bytes | None = None) -> str:
    encryption = (
        serialization.BestAvailableEncryption(passphrase)
        if passphrase
        else serialization.NoEncryption()
    )
    return (
        Ed25519PrivateKey.generate()
        .private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, encryption)
        .decode()
    )


def _paramiko_ed25519() -> paramiko.PKey:
    return paramiko.Ed25519Key.from_private_key(io.StringIO(_openssh_private_key()))
