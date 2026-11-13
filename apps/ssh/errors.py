"""SSH error catalog: every failure has a code, a human message and a hint on what to do."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _


class ErrorCode(StrEnum):
    HOST_UNREACHABLE = "host_unreachable"
    CONNECTION_TIMEOUT = "connection_timeout"
    PROTOCOL_ERROR = "protocol_error"
    AUTH_FAILED = "auth_failed"
    PRIVATE_KEY_INVALID = "private_key_invalid"
    PRIVATE_KEY_PASSPHRASE = "private_key_passphrase"
    HOST_KEY_MISMATCH = "host_key_mismatch"
    CONNECTION_LOST = "connection_lost"
    COMMAND_TIMEOUT = "command_timeout"
    COMMAND_FAILED = "command_failed"
    SUDO_NOT_INSTALLED = "sudo_not_installed"
    USER_NOT_IN_SUDOERS = "user_not_in_sudoers"
    SUDO_PASSWORD_REQUIRED = "sudo_password_required"
    DOCKER_NOT_INSTALLED = "docker_not_installed"
    CONTAINER_NOT_FOUND = "container_not_found"
    CONTAINER_NOT_RUNNING = "container_not_running"
    REMOTE_FILE_NOT_FOUND = "remote_file_not_found"
    SERVER_BUSY = "server_busy"
    FLOCK_NOT_INSTALLED = "flock_not_installed"


@dataclass(frozen=True)
class ErrorDescription:
    message: str | Promise
    hint: str | Promise


class CatalogError[C: StrEnum](Exception):
    """An error whose code a `catalog` explains to people with a message and a hint.

    `detail` is technical text for logs, never shown as is. The operation runner records
    `code`, `message` and `hint` of a failed operation.
    """

    catalog: ClassVar[Mapping[Any, ErrorDescription]]

    def __init__(self, code: C, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else str(code))

    @property
    def message(self) -> str:
        return str(self.catalog[self.code].message)

    @property
    def hint(self) -> str:
        return str(self.catalog[self.code].hint)


ERROR_CATALOG: dict[ErrorCode, ErrorDescription] = {
    ErrorCode.HOST_UNREACHABLE: ErrorDescription(
        _("The server cannot be reached."),
        _("Check the address and port, that the server is up and the firewall allows SSH."),
    ),
    ErrorCode.CONNECTION_TIMEOUT: ErrorDescription(
        _("The server did not answer in time."),
        _("Check that the server is online and the SSH port is open, then try again."),
    ),
    ErrorCode.PROTOCOL_ERROR: ErrorDescription(
        _("The server does not respond like an SSH server."),
        _("Check that the port belongs to SSH and not to another service."),
    ),
    ErrorCode.AUTH_FAILED: ErrorDescription(
        _("The server rejected the login."),
        _("Check the user name and the password or key. Make sure the key is in authorized_keys."),
    ),
    ErrorCode.PRIVATE_KEY_INVALID: ErrorDescription(
        _("The private key cannot be read."),
        _("Paste the whole key in OpenSSH or PEM format (Ed25519, ECDSA or RSA)."),
    ),
    ErrorCode.PRIVATE_KEY_PASSPHRASE: ErrorDescription(
        _("The private key is protected by a passphrase that is missing or wrong."),
        _("Enter the key passphrase."),
    ),
    ErrorCode.HOST_KEY_MISMATCH: ErrorDescription(
        _("The server identity has changed since the first connection."),
        _(
            "If the server was reinstalled, press Forget next to the SSH fingerprint "
            "on the server card. "
            "Otherwise the connection may be intercepted: do not continue."
        ),
    ),
    ErrorCode.CONNECTION_LOST: ErrorDescription(
        _("The connection to the server was lost."),
        _("Check the network and try again."),
    ),
    ErrorCode.COMMAND_TIMEOUT: ErrorDescription(
        _("The command on the server took too long."),
        _("The server may be overloaded. Try again later."),
    ),
    ErrorCode.COMMAND_FAILED: ErrorDescription(
        _("A command on the server failed."),
        _("See the operation log for details."),
    ),
    ErrorCode.SUDO_NOT_INSTALLED: ErrorDescription(
        _("sudo is not installed on the server."),
        _("Install sudo or connect as root."),
    ),
    ErrorCode.USER_NOT_IN_SUDOERS: ErrorDescription(
        _("The user is not allowed to use sudo."),
        _("Add the user to the sudo (or wheel) group, or connect as root."),
    ),
    ErrorCode.SUDO_PASSWORD_REQUIRED: ErrorDescription(
        _("sudo asks for a password."),
        _("Allow sudo without a password for this user (NOPASSWD in sudoers), or connect as root."),
    ),
    ErrorCode.DOCKER_NOT_INSTALLED: ErrorDescription(
        _("Docker is not installed on the server."),
        _("Install the server with the Amnezia app or the panel first."),
    ),
    ErrorCode.CONTAINER_NOT_FOUND: ErrorDescription(
        _("The protocol container was not found on the server."),
        _("The protocol may have been removed in the Amnezia app. Refresh the server data."),
    ),
    ErrorCode.CONTAINER_NOT_RUNNING: ErrorDescription(
        _("The protocol container is stopped."),
        _("Start the container on the server or in the Amnezia app."),
    ),
    ErrorCode.REMOTE_FILE_NOT_FOUND: ErrorDescription(
        _("A required file is missing on the server."),
        _("The protocol may be installed incompletely. Reinstall it or refresh the server data."),
    ),
    ErrorCode.SERVER_BUSY: ErrorDescription(
        _("Another operation is changing this server right now."),
        _("Wait for it to finish and try again."),
    ),
    ErrorCode.FLOCK_NOT_INSTALLED: ErrorDescription(
        _("The flock utility is missing on the server."),
        _("Install the util-linux package."),
    ),
}


class SshError(CatalogError[ErrorCode]):
    """Base error of the SSH layer."""

    catalog = ERROR_CATALOG


class SshConnectionError(SshError):
    """Connecting or logging in failed."""


class SshCommandError(SshError):
    """A remote command failed, timed out or lost its connection."""


class SshPrivilegeError(SshError):
    """The user cannot run commands as root."""


class DockerError(SshError):
    """Docker or the protocol container is not available."""


class RemoteFileNotFoundError(SshError):
    def __init__(self, path: str) -> None:
        super().__init__(ErrorCode.REMOTE_FILE_NOT_FOUND, path)


class ServerLockError(SshError):
    """The server lock could not be taken."""
