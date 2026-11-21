import io
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings
from django.core.management import call_command
from django.test import Client

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.backups.errors import PanelArchiveError, PanelArchiveErrorCode
from apps.backups.operations import archive_panel
from apps.backups.services.panel_archive import PanelArchiveReader
from apps.backups.services.panel_archives import (
    PANEL_ARCHIVE_KIND,
    PASSWORD_PARAM,
    start_panel_archive,
)
from apps.backups.tests.panel_archive_helpers import PASSWORD, FakeDump, downgrade, folder
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.ssh.encryption import SecretCipher

pytestmark = pytest.mark.django_db

UNPACK = settings.BASE_DIR / "deploy" / "archive" / "unpack.sh"


def admin_client(client: Client) -> Client:
    client.force_login(UserFactory(role=Role.ADMIN))
    return client


def test_the_password_travels_encrypted_and_one_archive_runs_at_a_time() -> None:
    admin = UserFactory(role=Role.ADMIN)

    operation = start_panel_archive(PASSWORD, admin, "10.0.0.1")

    token = operation.params[PASSWORD_PARAM]
    assert PASSWORD not in str(operation.params)
    assert SecretCipher.from_settings().decrypt(token) == PASSWORD
    assert start_panel_archive("other password", admin, None) == operation
    assert AuditEvent.objects.filter(action=PANEL_ARCHIVE_KIND).count() == 1


def test_the_operation_forgets_the_password_and_writes_the_archive(
    archive_settings: Path, fake_dump: FakeDump
) -> None:
    operation = start_panel_archive(PASSWORD, UserFactory(role=Role.ADMIN), None)

    archive_panel(OperationContext(operation))

    operation.refresh_from_db()
    assert PASSWORD_PARAM not in operation.params
    (archive,) = folder().archives()
    assert PanelArchiveReader(PASSWORD).check(archive.path)


def test_a_retry_without_the_password_fails() -> None:
    operation = Operation.objects.create(kind=PANEL_ARCHIVE_KIND, params={})

    with pytest.raises(PanelArchiveError) as error:
        archive_panel(OperationContext(operation))
    assert error.value.code == PanelArchiveErrorCode.NO_PASSWORD


def test_the_page_starts_an_archive(client: Client, archive_settings: Path) -> None:
    admin_client(client)
    assert client.get("/backups/panel/").status_code == 200

    differ = client.post("/backups/panel/", {"password": PASSWORD, "confirmation": "x" * 12})
    assert differ.status_code == 200
    assert not Operation.objects.exists()

    done = client.post("/backups/panel/", {"password": PASSWORD, "confirmation": PASSWORD})
    operation = Operation.objects.get(kind=PANEL_ARCHIVE_KIND)
    assert done["Location"] == f"/operations/{operation.pk}/"


def test_download_and_delete_are_audited(client: Client, archive_settings: Path) -> None:
    archive, _ = folder().create(PASSWORD, FakeDump())
    admin_client(client)

    response = client.get(f"/backups/panel/{archive.name}/download/")
    assert b"".join(response.streaming_content) == archive.path.read_bytes()  # type: ignore[attr-defined]
    client.post(f"/backups/panel/{archive.name}/delete/")

    assert not folder().archives()
    assert set(AuditEvent.objects.values_list("action", flat=True)) == {
        "backups.download_panel_archive",
        "backups.delete_panel_archive",
    }
    assert client.get("/backups/panel/lamnezia-1.lamnezia/download/").status_code == 404


def test_only_admins_see_archives(client: Client) -> None:
    client.force_login(UserFactory(role=Role.USER))

    assert client.get("/backups/panel/").status_code == 403


def test_the_command_makes_and_checks_an_archive(
    archive_settings: Path, fake_dump: FakeDump
) -> None:
    out = io.StringIO()
    call_command(
        "panel_archive", "create", "--password-stdin", stdin=io.StringIO(PASSWORD), stdout=out
    )
    (archive,) = folder().archives()

    call_command(
        "panel_archive", "verify", str(archive.path), "--password-stdin",
        stdin=io.StringIO(PASSWORD), stdout=out,
    )  # fmt: skip

    assert "The archive is whole" in out.getvalue()


def unpack(
    archive: Path, target: Path, password: str, code: Path | None = None
) -> subprocess.CompletedProcess[str]:
    command = [
        shutil.which("bash") or "bash",
        UNPACK.as_posix(),
        archive.as_posix(),
        target.as_posix(),
    ]
    if code is not None:
        command.append(code.as_posix())
    environment = {**os.environ, "LAMNEZIA_ARCHIVE_PASSWORD": password}
    return subprocess.run(command, env=environment, capture_output=True, text=True, check=False)


needs_tools = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("bash", "openssl", "tar", "sha256sum")),
    reason="no bash, openssl, tar or sha256sum",
)
# unpack.sh hands the password to openssl as `-pass fd:3`: stdin carries the ciphertext, and
# the password stays off the command line and the environment. OpenSSL on Windows (Git Bash
# ships a native build) has no `fd:` source, and the script is for the Linux server anyway.
linux_script = pytest.mark.skipif(
    sys.platform == "win32", reason="unpack.sh needs openssl with `-pass fd:` (no Windows)"
)


@linux_script
@needs_tools
def test_unpack_script_restores_the_files(archive_settings: Path) -> None:
    archive, _ = folder().create(PASSWORD, FakeDump())
    target = archive_settings / "restore"

    done = unpack(archive.path, target, PASSWORD, settings.BASE_DIR)

    assert done.returncode == 0, done.stderr
    assert (target / "database.dump").read_bytes() == FakeDump().content
    assert (target / "media" / "logos" / "logo.png").read_bytes() == b"png"


@linux_script
@needs_tools
def test_unpack_script_refuses_a_wrong_password_and_a_newer_panel(archive_settings: Path) -> None:
    archive, _ = folder().create(PASSWORD, FakeDump())

    wrong = unpack(archive.path, archive_settings / "wrong", "another password")
    assert wrong.returncode != 0
    assert "wrong archive password" in wrong.stderr

    older_code = archive_settings / "old-code"
    older_code.mkdir()
    newer = unpack(archive.path, archive_settings / "newer", PASSWORD, older_code)
    assert newer.returncode != 0
    assert "newer panel" in newer.stderr


@linux_script
@needs_tools
def test_unpack_script_tells_a_damaged_archive(archive_settings: Path) -> None:
    archive, _ = folder().create(PASSWORD, FakeDump(os.urandom(100_000)))
    raw = bytearray(archive.path.read_bytes())
    raw[50_000] ^= 1
    archive.path.write_bytes(bytes(raw))

    for password in (PASSWORD, "another password"):
        done = unpack(archive.path, archive_settings / "damaged", password)
        assert done.returncode != 0
        assert ("damaged" in done.stderr) is (password == PASSWORD), done.stderr
    assert not (archive_settings / "damaged" / "database.dump").exists()


@linux_script
@needs_tools
def test_unpack_script_restores_an_archive_of_format_1(archive_settings: Path) -> None:
    archive, _ = folder().create(PASSWORD, FakeDump())
    downgrade(archive.path, PASSWORD)
    target = archive_settings / "restore"

    wrong = unpack(archive.path, archive_settings / "wrong", "another password")
    done = unpack(archive.path, target, PASSWORD, settings.BASE_DIR)

    assert "cannot be read with this password, or the file is damaged" in wrong.stderr
    assert done.returncode == 0, done.stderr
    assert (target / "database.dump").read_bytes() == FakeDump().content
