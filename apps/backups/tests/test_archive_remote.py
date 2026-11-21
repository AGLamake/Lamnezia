import io
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from apps.backups.errors import PanelArchiveError, PanelArchiveErrorCode
from apps.backups.services.archive_remote import (
    S3Remote,
    SftpLogin,
    SftpRemote,
    SigV4,
    check_host_key,
    remote_from_settings,
)

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
HOST_KEY = "AAAAC3NzaC1lZDI1NTE5AAAAIExample"
LOGIN = SftpLogin(
    "backup.example", 2222, "keeper", "sftp secret", f"[backup.example]:2222 ssh-ed25519 {HOST_KEY}"
)


def test_sigv4_matches_the_aws_test_suite() -> None:
    signer = SigV4(
        "AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "us-east-1", "service"
    )
    headers = {"Host": "example.amazonaws.com", "X-Amz-Date": "20150830T123600Z"}

    authorization = signer.authorization("GET", "/", headers, EMPTY_SHA256)

    assert authorization == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
        "SignedHeaders=host;x-amz-date, "
        "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    )


class FakeHttp:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.requests: list[tuple[Request, bytes]] = []

    def __call__(self, request: Request, timeout: float) -> None:
        self.requests.append((request, request.data.read()))  # type: ignore[union-attr]
        if self.error is not None:
            raise self.error


def s3(http: FakeHttp) -> S3Remote:
    signer = SigV4("access", "secret key", "eu-central-1")
    return S3Remote("https://storage.example:9000", "backups", "panel/", signer, http)


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    path = tmp_path / "lamnezia-20261121-0300.tar.gz.enc"
    path.write_bytes(b"encrypted archive")
    return path


def test_s3_puts_the_file_signed_under_its_name(archive: Path) -> None:
    http = FakeHttp()

    s3(http).send(archive)

    ((request, body),) = http.requests
    assert request.get_method() == "PUT"
    assert request.full_url == f"https://storage.example:9000/backups/panel/{archive.name}"
    assert body == b"encrypted archive"
    headers = {name.lower(): value for name, value in request.header_items()}
    assert headers["host"] == "storage.example:9000"
    assert headers["content-length"] == str(len(body))
    assert headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=access/")
    assert "secret key" not in str(headers)


def http_error(status: int, body: bytes) -> HTTPError:
    return HTTPError("https://storage.example", status, "error", {}, io.BytesIO(body))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("status", "code"),
    [(403, PanelArchiveErrorCode.REMOTE_LOGIN), (404, PanelArchiveErrorCode.REMOTE_REFUSED)],
)
def test_s3_tells_a_refusal_by_its_code_only(
    archive: Path, status: int, code: PanelArchiveErrorCode
) -> None:
    body = b"<Error><Code>AccessDenied</Code><Message>long text</Message></Error>"

    with pytest.raises(PanelArchiveError) as error:
        s3(FakeHttp(http_error(status, body))).send(archive)

    assert error.value.code == code
    assert error.value.detail == f"{status} AccessDenied"


def test_s3_tells_an_unreachable_storage(archive: Path) -> None:
    with pytest.raises(PanelArchiveError) as error:
        s3(FakeHttp(URLError("Name or service not known"))).send(archive)

    assert error.value.code == PanelArchiveErrorCode.REMOTE_UNREACHABLE
    assert error.value.detail == "Name or service not known"


class FakeSftp:
    def __init__(self, error: OSError | None = None) -> None:
        self.error = error
        self.files: dict[str, bytes] = {}
        self.logins: list[SftpLogin] = []

    @contextmanager
    def session(self, login: SftpLogin) -> Iterator["FakeSftp"]:
        self.logins.append(login)
        yield self

    def put(self, localpath: str, remotepath: str) -> object:
        if self.error is not None:
            raise self.error
        self.files[remotepath] = Path(localpath).read_bytes()
        return None

    def posix_rename(self, oldpath: str, newpath: str) -> None:
        self.files[newpath] = self.files.pop(oldpath)


def test_sftp_shows_the_copy_only_when_complete(archive: Path) -> None:
    server = FakeSftp()

    SftpRemote(LOGIN, "archives/", server.session).send(archive)

    assert server.files == {f"archives/{archive.name}": b"encrypted archive"}
    assert server.logins == [LOGIN]


def test_sftp_tells_a_refused_write(archive: Path) -> None:
    remote = SftpRemote(LOGIN, "", FakeSftp(PermissionError("Permission denied")).session)

    with pytest.raises(PanelArchiveError) as error:
        remote.send(archive)

    assert error.value.code == PanelArchiveErrorCode.REMOTE_REFUSED


def test_sftp_needs_the_whole_login() -> None:
    login = SftpLogin("backup.example", 22, "keeper", "secret", "")

    with pytest.raises(PanelArchiveError) as error:
        SftpRemote(login, "")

    assert error.value.code == PanelArchiveErrorCode.REMOTE_INVALID


def test_the_host_key_must_be_the_expected_one() -> None:
    check_host_key("ssh-ed25519", HOST_KEY, LOGIN.host_key)
    check_host_key("ssh-ed25519", HOST_KEY, f"ssh-ed25519 {HOST_KEY}")

    with pytest.raises(PanelArchiveError) as error:
        check_host_key("ssh-ed25519", "AAAAother", LOGIN.host_key)

    assert error.value.code == PanelArchiveErrorCode.REMOTE_HOST_KEY
    assert error.value.detail == "ssh-ed25519 AAAAother"


def test_no_storage_by_default(settings: Any) -> None:
    settings.PANEL_ARCHIVE_REMOTE = ""

    assert remote_from_settings() is None


def test_an_s3_storage_from_the_settings(settings: Any) -> None:
    settings.PANEL_ARCHIVE_REMOTE = "s3://backups/lamnezia/panel"
    settings.PANEL_ARCHIVE_REMOTE_ACCESS_KEY = "access"
    settings.PANEL_ARCHIVE_REMOTE_SECRET_KEY = "secret"

    remote = remote_from_settings()

    assert remote is not None
    assert remote.label == "s3://backups/lamnezia/panel/"


def test_an_sftp_storage_from_the_settings(settings: Any) -> None:
    settings.PANEL_ARCHIVE_REMOTE = "sftp://keeper@backup.example:2222//srv/archives"
    settings.PANEL_ARCHIVE_REMOTE_PASSWORD = "sftp secret"
    settings.PANEL_ARCHIVE_REMOTE_HOST_KEY = f"ssh-ed25519 {HOST_KEY}"

    remote = remote_from_settings()

    assert remote is not None
    assert remote.label == "sftp://keeper@backup.example:2222//srv/archives/"


@pytest.mark.parametrize(
    "url",
    ["ftp://host/folder", "s3://bucket/folder", "sftp://host/folder", "sftp://keeper@host:port/x"],
)
def test_wrong_storage_settings(settings: Any, url: str) -> None:
    settings.PANEL_ARCHIVE_REMOTE = url
    settings.PANEL_ARCHIVE_REMOTE_ACCESS_KEY = ""
    settings.PANEL_ARCHIVE_REMOTE_PASSWORD = "secret"
    settings.PANEL_ARCHIVE_REMOTE_HOST_KEY = f"ssh-ed25519 {HOST_KEY}"

    with pytest.raises(PanelArchiveError) as error:
        remote_from_settings()

    assert error.value.code == PanelArchiveErrorCode.REMOTE_INVALID
