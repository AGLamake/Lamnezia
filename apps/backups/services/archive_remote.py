"""Copies of the panel archive in an external storage: S3-compatible or SFTP, from the settings.

A copy shows up under its own name only when it is complete: S3 keeps no object of a broken
upload, and SFTP writes a ``.part`` file and renames it. Old copies are never removed: the
storage keeps them as long as its owner wants. No secret goes into a label or an error detail.
"""

import hashlib
import hmac
import re
import socket
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

import paramiko
from django.conf import settings
from django.utils import timezone

from apps.backups.errors import PanelArchiveError, PanelArchiveErrorCode

_S3_ERROR_CODE = re.compile(rb"<Code>([^<]{1,64})</Code>")
_KEY_TYPE = re.compile(r"(ssh-|ecdsa-|sk-)\S+")


class ArchiveRemote(Protocol):
    """Where the copies go; `label` names it for people and holds no secret."""

    @property
    def label(self) -> str: ...

    def send(self, path: Path) -> None:
        """Put the file there under its own name; failures raise `PanelArchiveError`."""


@dataclass(frozen=True)
class SigV4:
    """AWS Signature Version 4 of a request, as S3-compatible storages check it."""

    access_key: str
    secret_key: str
    region: str
    service: str = "s3"

    def authorization(
        self, method: str, path: str, headers: Mapping[str, str], payload_hash: str
    ) -> str:
        """The Authorization header; `headers` are all signed and hold `X-Amz-Date`."""
        values = {name.lower(): value.strip() for name, value in headers.items()}
        names = sorted(values)
        signed = ";".join(names)
        lines = [f"{name}:{values[name]}" for name in names]
        canonical = "\n".join([method, path, "", *lines, "", signed, payload_hash])
        stamp = values["x-amz-date"]
        scope = f"{stamp[:8]}/{self.region}/{self.service}/aws4_request"
        text = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, _sha256(canonical.encode())])
        key = f"AWS4{self.secret_key}".encode()
        for part in scope.split("/"):
            key = hmac.new(key, part.encode(), hashlib.sha256).digest()
        signature = hmac.new(key, text.encode(), hashlib.sha256).hexdigest()
        return (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed}, Signature={signature}"
        )


# Sends a request with a timeout in seconds; an HTTP error raises `HTTPError`.
UrlOpener = Callable[[Request, float], None]


def _open_url(request: Request, timeout: float) -> None:
    with urlopen(request, timeout=timeout):
        pass


class S3Remote:
    """A bucket of an S3-compatible storage, addressed by path: ``endpoint/bucket/folder/name``."""

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        folder: str,
        signer: SigV4,
        open_url: UrlOpener = _open_url,
    ) -> None:
        parts = urlsplit(endpoint)
        if parts.scheme not in ("http", "https") or not parts.netloc or not bucket:
            raise PanelArchiveError(PanelArchiveErrorCode.REMOTE_INVALID, "s3 endpoint or bucket")
        self._origin = f"{parts.scheme}://{parts.netloc}"
        self._host = parts.netloc
        self._bucket = bucket
        self._folder = folder
        self._signer = signer
        self._open_url = open_url

    @property
    def label(self) -> str:
        return f"s3://{self._bucket}/{self._folder}"

    def send(self, path: Path) -> None:
        target = "/" + quote(f"{self._bucket}/{self._folder}{path.name}", safe="/-_.~")
        payload_hash = _file_sha256(path)
        headers = {
            "Host": self._host,
            "X-Amz-Content-Sha256": payload_hash,
            "X-Amz-Date": timezone.now().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ"),
        }
        headers["Authorization"] = self._signer.authorization("PUT", target, headers, payload_hash)
        headers["Content-Length"] = str(path.stat().st_size)
        with path.open("rb") as body:
            request = Request(self._origin + target, data=body, headers=headers, method="PUT")
            try:
                self._open_url(request, settings.PANEL_ARCHIVE_REMOTE_TIMEOUT)
            except HTTPError as exc:
                code = PanelArchiveErrorCode.REMOTE_REFUSED
                if exc.code in (401, 403):
                    code = PanelArchiveErrorCode.REMOTE_LOGIN
                raise PanelArchiveError(code, _http_answer(exc)) from exc
            except (URLError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                raise PanelArchiveError(
                    PanelArchiveErrorCode.REMOTE_UNREACHABLE, str(reason)
                ) from exc


class SftpFiles(Protocol):
    """What a copy needs of an SFTP session (`paramiko.SFTPClient`)."""

    def put(self, localpath: str, remotepath: str) -> object: ...

    def posix_rename(self, oldpath: str, newpath: str) -> None: ...


@dataclass(frozen=True)
class SftpLogin:
    host: str
    port: int
    user: str
    password: str
    host_key: str

    @property
    def address(self) -> str:
        return f"{self.user}@{self.host}:{self.port}"


@contextmanager
def open_sftp(login: SftpLogin) -> Iterator[SftpFiles]:
    """A session with the server whose key is `login.host_key`, logged in with the password."""
    timeout = settings.PANEL_ARCHIVE_REMOTE_TIMEOUT
    try:
        sock = socket.create_connection((login.host, login.port), timeout=timeout)
    except OSError as exc:
        raise PanelArchiveError(PanelArchiveErrorCode.REMOTE_UNREACHABLE, str(exc)) from exc
    transport = paramiko.Transport(sock)
    try:
        try:
            transport.start_client(timeout=timeout)
        except paramiko.SSHException as exc:
            raise PanelArchiveError(PanelArchiveErrorCode.REMOTE_UNREACHABLE, str(exc)) from exc
        presented = transport.get_remote_server_key()
        check_host_key(presented.get_name(), presented.get_base64(), login.host_key)
        try:
            transport.auth_password(login.user, login.password)
        except paramiko.AuthenticationException as exc:
            raise PanelArchiveError(PanelArchiveErrorCode.REMOTE_LOGIN, login.address) from exc
        yield paramiko.SFTPClient.from_transport(transport)  # type: ignore[misc]
    finally:
        transport.close()


def check_host_key(name: str, key: str, expected: str) -> None:
    """The presented key must be the expected one: a line of ``ssh-keyscan`` or its two words."""
    words = expected.split()
    known = [(word, words[i + 1]) for i, word in enumerate(words[:-1]) if _KEY_TYPE.fullmatch(word)]
    if (name, key) not in known:
        raise PanelArchiveError(PanelArchiveErrorCode.REMOTE_HOST_KEY, f"{name} {key}")


SftpOpener = Callable[[SftpLogin], AbstractContextManager[SftpFiles]]


class SftpRemote:
    """A folder on an SFTP server, from the login folder of the user unless it starts with /."""

    def __init__(self, login: SftpLogin, folder: str, open_session: SftpOpener = open_sftp) -> None:
        if not login.host or not login.user or not login.password or not login.host_key:
            raise PanelArchiveError(PanelArchiveErrorCode.REMOTE_INVALID, "sftp login")
        self._login = login
        self._folder = folder
        self._open_session = open_session

    @property
    def label(self) -> str:
        return f"sftp://{self._login.address}/{self._folder}"

    def send(self, path: Path) -> None:
        target = f"{self._folder}{path.name}"
        with self._open_session(self._login) as files:
            try:
                files.put(str(path), f"{target}.part")
                files.posix_rename(f"{target}.part", target)
            except OSError as exc:
                raise PanelArchiveError(PanelArchiveErrorCode.REMOTE_REFUSED, str(exc)) from exc


def remote_from_settings() -> ArchiveRemote | None:
    """The storage of LAMNEZIA_PANEL_ARCHIVE_REMOTE; None when copies are not sent anywhere."""
    url = settings.PANEL_ARCHIVE_REMOTE.strip()
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme == "s3":
        signer = SigV4(
            settings.PANEL_ARCHIVE_REMOTE_ACCESS_KEY,
            settings.PANEL_ARCHIVE_REMOTE_SECRET_KEY,
            settings.PANEL_ARCHIVE_REMOTE_REGION,
        )
        if not signer.access_key or not signer.secret_key:
            raise PanelArchiveError(PanelArchiveErrorCode.REMOTE_INVALID, "s3 keys")
        bucket = parts.netloc
        return S3Remote(settings.PANEL_ARCHIVE_REMOTE_ENDPOINT, bucket, _folder(parts.path), signer)
    if parts.scheme == "sftp":
        try:
            port = parts.port or 22
        except ValueError as exc:
            raise PanelArchiveError(PanelArchiveErrorCode.REMOTE_INVALID, "sftp port") from exc
        login = SftpLogin(
            parts.hostname or "",
            port,
            unquote(parts.username or ""),
            settings.PANEL_ARCHIVE_REMOTE_PASSWORD or unquote(parts.password or ""),
            settings.PANEL_ARCHIVE_REMOTE_HOST_KEY,
        )
        return SftpRemote(login, _folder(parts.path, relative=True))
    raise PanelArchiveError(PanelArchiveErrorCode.REMOTE_INVALID, f"scheme {parts.scheme!r}")


def _folder(path: str, *, relative: bool = False) -> str:
    """The folder with a trailing slash, or empty; an SFTP path keeps a second leading slash."""
    if relative and path.startswith("//"):
        return "/" + _folder(path)
    folder = unquote(path).strip("/")
    return f"{folder}/" if folder else ""


def _http_answer(error: HTTPError) -> str:
    """The status and the S3 error code, e.g. ``403 AccessDenied``; never the whole body."""
    try:
        match = _S3_ERROR_CODE.search(error.read(4096))
    except OSError:
        match = None
    return f"{error.code} {match.group(1).decode(errors='replace')}" if match else str(error.code)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()
