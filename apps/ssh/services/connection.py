"""SSH connection on paramiko: login, host key check, commands with timeouts."""

import io
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self

import paramiko
from django.conf import settings

from apps.ssh.errors import ErrorCode, SshCommandError, SshConnectionError, SshError
from apps.ssh.services.commands import CommandResult, CommandRunner, RemoteProcess
from apps.ssh.services.host_keys import KnownHostVerifier

_READ_CHUNK = 65536
_POLL_INTERVAL = 0.02
# Keeps idle sessions (a held server lock) alive behind NAT and firewalls.
_KEEPALIVE_INTERVAL = 30
_KEY_CLASSES: tuple[type[paramiko.PKey], ...] = (
    paramiko.Ed25519Key,
    paramiko.ECDSAKey,
    paramiko.RSAKey,
)


@dataclass(frozen=True)
class SshCredentials:
    host: str
    port: int
    username: str
    password: str = field(default="", repr=False)
    private_key: str = field(default="", repr=False)
    passphrase: str = field(default="", repr=False)


@dataclass(frozen=True)
class SshTimeouts:
    """Seconds. `command` bounds one command, `lock` bounds waiting for the server lock."""

    connect: float
    command: float
    lock: float

    @classmethod
    def from_settings(cls) -> Self:
        return cls(
            connect=settings.SSH_CONNECT_TIMEOUT,
            command=settings.SSH_COMMAND_TIMEOUT,
            lock=settings.SSH_LOCK_TIMEOUT,
        )


class SshConnection(CommandRunner):
    """One SSH session to a server; use as a context manager."""

    def __init__(
        self,
        credentials: SshCredentials,
        *,
        timeouts: SshTimeouts | None = None,
        verifier: KnownHostVerifier | None = None,
    ) -> None:
        self._credentials = credentials
        self.timeouts = timeouts or SshTimeouts.from_settings()
        self._verifier = verifier or KnownHostVerifier()
        self._client: paramiko.SSHClient | None = None

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def connect(self) -> None:
        creds = self._credentials
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(_VerifyingPolicy(self._verifier, creds.host, creds.port))
        try:
            client.connect(
                creds.host,
                port=creds.port,
                username=creds.username,
                password=creds.password or None,
                pkey=_load_private_key(creds) if creds.private_key else None,
                timeout=self.timeouts.connect,
                banner_timeout=self.timeouts.connect,
                auth_timeout=self.timeouts.connect,
                allow_agent=False,
                look_for_keys=False,
            )
        except SshError:
            client.close()
            raise
        except (paramiko.SSHException, OSError) as exc:
            client.close()
            raise _connection_error(exc) from exc
        transport = client.get_transport()
        if transport is not None:
            transport.set_keepalive(_KEEPALIVE_INTERVAL)
        self._client = client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def run(
        self, command: str, *, stdin: bytes = b"", timeout: float | None = None
    ) -> CommandResult:
        timeout = timeout or self.timeouts.command
        channel = self._open_channel(command, timeout)
        try:
            if stdin:
                channel.sendall(stdin)
            channel.shutdown_write()
            return _collect(channel, time.monotonic() + timeout)
        except (TimeoutError, OSError, paramiko.SSHException, EOFError) as exc:
            raise _command_error(exc) from exc
        finally:
            channel.close()

    def start(self, command: str) -> RemoteProcess:
        return _ChannelProcess(self._open_channel(command, self.timeouts.command))

    def _open_channel(self, command: str, timeout: float) -> paramiko.Channel:
        transport = self._client.get_transport() if self._client else None
        if transport is None or not transport.is_active():
            raise SshCommandError(ErrorCode.CONNECTION_LOST, "not connected")
        try:
            channel = transport.open_session(timeout=timeout)
            channel.settimeout(timeout)
            channel.exec_command(command)
        except (OSError, paramiko.SSHException, EOFError) as exc:
            raise _command_error(exc) from exc
        return channel


class _VerifyingPolicy(paramiko.MissingHostKeyPolicy):
    # No known_hosts is loaded into paramiko, so every key reaches this policy.
    def __init__(self, verifier: KnownHostVerifier, host: str, port: int) -> None:
        self._verifier = verifier
        self._host = host
        self._port = port

    def missing_host_key(
        self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey
    ) -> None:
        self._verifier.verify(self._host, self._port, key.get_name(), key.fingerprint)


class _ChannelProcess(RemoteProcess):
    def __init__(self, channel: paramiko.Channel) -> None:
        self._channel = channel
        self._buffer = bytearray()

    def read_line(self, timeout: float) -> str | None:
        def line_or_end() -> bool:
            while self._channel.recv_ready():
                self._buffer += self._channel.recv(_READ_CHUNK)
            return b"\n" in self._buffer or self._channel.exit_status_ready()

        _poll_until(line_or_end, time.monotonic() + timeout)
        if b"\n" not in self._buffer:
            return None
        line, _, rest = self._buffer.partition(b"\n")
        self._buffer = bytearray(rest)
        return line.decode(errors="replace")

    def close_stdin(self) -> None:
        self._channel.shutdown_write()

    def wait(self, timeout: float) -> int:
        _poll_until(self._channel.exit_status_ready, time.monotonic() + timeout)
        return self._channel.recv_exit_status()

    def close(self) -> None:
        self._channel.close()


def _load_private_key(creds: SshCredentials) -> paramiko.PKey:
    passphrase = creds.passphrase or None
    for key_class in _KEY_CLASSES:
        try:
            return key_class.from_private_key(io.StringIO(creds.private_key), password=passphrase)
        except paramiko.PasswordRequiredException as exc:
            raise SshConnectionError(ErrorCode.PRIVATE_KEY_PASSPHRASE) from exc
        except (paramiko.SSHException, ValueError):
            continue
    code = ErrorCode.PRIVATE_KEY_PASSPHRASE if passphrase else ErrorCode.PRIVATE_KEY_INVALID
    raise SshConnectionError(code)


def _collect(channel: paramiko.Channel, deadline: float) -> CommandResult:
    stdout, stderr = bytearray(), bytearray()

    def finished() -> bool:
        while channel.recv_ready():
            stdout.extend(channel.recv(_READ_CHUNK))
        while channel.recv_stderr_ready():
            stderr.extend(channel.recv_stderr(_READ_CHUNK))
        return channel.exit_status_ready() and not (
            channel.recv_ready() or channel.recv_stderr_ready()
        )

    _poll_until(finished, deadline)
    exit_code = channel.recv_exit_status()
    if exit_code == -1:  # paramiko: the channel closed without an exit status
        raise SshCommandError(ErrorCode.CONNECTION_LOST, "channel closed")
    return CommandResult(exit_code, bytes(stdout), bytes(stderr))


def _poll_until(condition: Callable[[], bool], deadline: float) -> None:
    # Polling keeps both output buffers drained, so a chatty command never stalls.
    while not condition():
        if time.monotonic() > deadline:
            raise SshCommandError(ErrorCode.COMMAND_TIMEOUT)
        time.sleep(_POLL_INTERVAL)


def _connection_error(exc: paramiko.SSHException | OSError) -> SshConnectionError:
    if isinstance(exc, paramiko.AuthenticationException):
        code = ErrorCode.AUTH_FAILED
    elif isinstance(exc, paramiko.SSHException):
        code = ErrorCode.PROTOCOL_ERROR
    elif isinstance(exc, TimeoutError):
        code = ErrorCode.CONNECTION_TIMEOUT
    else:
        code = ErrorCode.HOST_UNREACHABLE
    return SshConnectionError(code, str(exc))


def _command_error(exc: Exception) -> SshCommandError:
    code = ErrorCode.COMMAND_TIMEOUT if isinstance(exc, TimeoutError) else ErrorCode.CONNECTION_LOST
    return SshCommandError(code, str(exc))
