import pytest

from apps.ssh.errors import (
    DockerError,
    ErrorCode,
    RemoteFileNotFoundError,
    ServerLockError,
    SshCommandError,
    SshConnectionError,
    SshPrivilegeError,
)
from apps.ssh.models import KnownHost
from apps.ssh.services import RemoteHost
from apps.ssh.tests.integration.conftest import (
    CONTAINER,
    HOST,
    PORT,
    TIMEOUTS,
    ConnectionFactory,
    admin_credentials,
    plain_credentials,
)


def test_key_login_saves_host_key_and_reconnects(connect: ConnectionFactory) -> None:
    connect(admin_credentials())
    connect(admin_credentials())

    assert KnownHost.objects.filter(host=HOST, port=PORT).count() == 1


def test_changed_host_key_is_refused(connect: ConnectionFactory) -> None:
    KnownHost.objects.create(host=HOST, port=PORT, key_type="ssh-ed25519", fingerprint="SHA256:x")

    with pytest.raises(SshConnectionError) as error:
        connect(admin_credentials())

    assert error.value.code == ErrorCode.HOST_KEY_MISMATCH


def test_wrong_password(connect: ConnectionFactory) -> None:
    with pytest.raises(SshConnectionError) as error:
        connect(plain_credentials(password="wrong"))

    assert error.value.code == ErrorCode.AUTH_FAILED


def test_sudo_user_runs_as_root(host: RemoteHost) -> None:
    assert host.run("id -u").strip() == "0"


def test_user_without_sudo_is_explained(connect: ConnectionFactory) -> None:
    host = RemoteHost(connect(plain_credentials()), TIMEOUTS)

    with pytest.raises(SshPrivilegeError) as error:
        host.check_privileges()

    assert error.value.code == ErrorCode.USER_NOT_IN_SUDOERS


def test_command_timeout(host: RemoteHost) -> None:
    with pytest.raises(SshCommandError) as error:
        host.run("sleep 5", timeout=1)

    assert error.value.code == ErrorCode.COMMAND_TIMEOUT


def test_atomic_write_and_read_on_host(host: RemoteHost) -> None:
    data = b"[Interface]\nPrivateKey = test\n" * 5000

    host.write_file_atomic("/opt/amnezia/test/awg0.conf", data)

    assert host.read_file("/opt/amnezia/test/awg0.conf") == data
    assert host.run("stat -c %a /opt/amnezia/test/awg0.conf").strip() == "600"
    assert host.run("ls -A /opt/amnezia/test").split() == ["awg0.conf"]


def test_read_missing_file(host: RemoteHost) -> None:
    with pytest.raises(RemoteFileNotFoundError):
        host.read_file("/opt/amnezia/missing")


def test_atomic_write_and_read_in_container(host: RemoteHost) -> None:
    container = host.container(CONTAINER)

    container.write_file_atomic("/opt/amnezia/awg/clientsTable", b"[]")

    assert container.read_file("/opt/amnezia/awg/clientsTable") == b"[]"
    assert CONTAINER in host.docker("ps", "--format", "{{.Names}}")


def test_missing_container(host: RemoteHost) -> None:
    with pytest.raises(DockerError) as error:
        host.container("lamnezia-no-such-container").run("true")

    assert error.value.code == ErrorCode.CONTAINER_NOT_FOUND


def test_second_lock_waits_and_gives_up(host: RemoteHost, connect: ConnectionFactory) -> None:
    other = RemoteHost(connect(admin_credentials()), TIMEOUTS)

    with host.lock(), pytest.raises(ServerLockError) as error, other.lock():
        pass

    assert error.value.code == ErrorCode.SERVER_BUSY
    with other.lock():
        pass


def test_lock_is_freed_when_connection_dies(connect: ConnectionFactory) -> None:
    dying_connection = connect(admin_credentials())
    RemoteHost(dying_connection, TIMEOUTS).lock().__enter__()

    dying_connection.close()

    with RemoteHost(connect(admin_credentials()), TIMEOUTS).lock():
        pass
