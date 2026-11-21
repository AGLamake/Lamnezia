"""Service copies as files: unchanged and oversized files, old copies, interrupted restores."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import Client

from amnezia_core.container_archive import (
    MTPROXY_STATE,
    RESTORE_DONE,
    ArchiveFormatError,
    ArchiveTooLargeError,
    ContainerArchive,
    StateFingerprint,
)
from amnezia_core.services import SFTP_CONTAINER
from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.backups.models import ServiceBackup
from apps.backups.services import archived_services as archived_module
from apps.backups.services import snapshot
from apps.backups.services.archive_restore import ArchiveSeed
from apps.backups.services.sealed_files import MAGIC, is_sealed, is_stream_key
from apps.backups.services.service_copies import FOLDER, service_copies
from apps.backups.tests.fake_containers import TarServiceHost
from apps.backups.tests.test_services import CASES, OTHER_SECRET, installed, restore
from apps.servers.models import Server
from apps.ssh.services import CommandResult, RemoteHost

pytestmark = pytest.mark.django_db

UPLOAD = "/home/sftp_user/upload/report.pdf"


def copy_files(media_root: Path) -> list[Path]:
    return sorted((media_root / FOLDER).glob("*.bin"))


def test_fingerprint_command_and_its_answer() -> None:
    command = MTPROXY_STATE.fingerprint_command()

    assert "du -sk" in command
    assert "sha256sum" in command
    assert StateFingerprint.parse(f"3 {'a' * 64}\n") == StateFingerprint(3072, "a" * 64)
    for output in ("", "3", f"x {'a' * 64}", "3 abc"):
        with pytest.raises(ArchiveFormatError):
            StateFingerprint.parse(output)


def test_unchanged_files_are_not_copied_again(
    monkeypatch: pytest.MonkeyPatch, media_root: Path
) -> None:
    service, archived, fake = installed(CASES["sftp"])
    first = archived.save(service, fake.host())
    assert first is not None
    reads: list[object] = []
    monkeypatch.setattr(archived_module, "read_archive", lambda *args: reads.append(args))

    second = archived.save(service, fake.host())

    assert reads == []
    assert second is not None
    assert second.file.name == first.file.name
    assert second.unchanged_since
    assert len(copy_files(media_root)) == 1


def test_changed_files_replace_the_copy_and_its_file(
    media_root: Path, django_capture_on_commit_callbacks: Any
) -> None:
    service, archived, fake = installed(CASES["sftp"])
    first = archived.save(service, fake.host())
    fake.put(SFTP_CONTAINER, UPLOAD, "report")

    with django_capture_on_commit_callbacks(execute=True):
        second = archived.save(service, fake.host())

    assert first is not None
    assert second is not None
    assert second.digest != first.digest
    assert copy_files(media_root) == [media_root / str(second.file.name)]
    assert service_copies.archive(second, archived.state).files()[UPLOAD] == b"report"


def test_too_large_files_keep_the_last_copy(
    monkeypatch: pytest.MonkeyPatch, media_root: Path
) -> None:
    service, archived, fake = installed(CASES["sftp"])
    first = archived.save(service, fake.host())
    assert first is not None
    fake.put(SFTP_CONTAINER, UPLOAD, "x" * 4096)
    monkeypatch.setattr(archived, "state", replace(archived.state, max_size=2048))

    backup = archived.save(service, fake.host())

    assert backup is not None
    assert backup.too_large_size is not None
    assert backup.too_large_size > 2048
    assert backup.file.name == first.file.name
    assert backup.taken_at == first.taken_at
    assert len(copy_files(media_root)) == 1


def test_a_read_over_the_limit_is_noted(monkeypatch: pytest.MonkeyPatch) -> None:
    service, archived, fake = installed(CASES["mtproxy"])

    def too_large(*args: object) -> ContainerArchive:
        raise ArchiveTooLargeError(5000)

    monkeypatch.setattr(archived_module, "read_archive", too_large)
    monkeypatch.setattr(archived, "_fingerprint", lambda host: None)

    backup = archived.save(service, fake.host())

    assert backup is not None
    assert backup.too_large_size == 5000
    assert not backup.has_copy


def test_a_copy_that_fits_again_clears_the_warning() -> None:
    service, archived, fake = installed(CASES["mtproxy"])
    service_copies.too_large(service, 5000)

    backup = archived.save(service, fake.host())

    assert backup is not None
    assert backup.has_copy
    assert backup.too_large_at is None


def test_server_with_only_a_size_warning_is_deleted() -> None:
    service, _archived, _fake = installed(CASES["sftp"])
    service_copies.too_large(service, 5000)

    service.server.delete()

    assert not Server.objects.exists()
    assert not ServiceBackup.objects.exists()


def test_copy_of_an_older_panel_is_restored() -> None:
    service, archived, fake = installed(CASES["mtproxy"])
    saved = archived.save(service, fake.host())
    assert saved is not None
    data = service_copies.archive(saved, archived.state)
    saved.delete()
    ServiceBackup.objects.create(service=service, archive=data.encode(), taken_at=saved.taken_at)

    clean = TarServiceHost(own_key="c", new_secret=OTHER_SECRET)
    restore(service, archived, clean)

    state = archived.state
    assert (
        clean.container_files(state.container)[state.identity_path]
        == fake.container_files(state.container)[state.identity_path]
    )


def test_deleted_copy_takes_its_file_along(
    media_root: Path, django_capture_on_commit_callbacks: Any
) -> None:
    service, archived, fake = installed(CASES["telemt"])
    archived.save(service, fake.host())
    assert len(copy_files(media_root)) == 1

    with django_capture_on_commit_callbacks(execute=True):
        service.delete()

    assert copy_files(media_root) == []


def test_unreadable_file_is_an_archive_error() -> None:
    service, archived, fake = installed(CASES["mtproxy"])
    backup = archived.save(service, fake.host())
    assert backup is not None
    default_storage.delete(str(backup.file.name))

    with pytest.raises(ArchiveFormatError):
        service_copies.archive(backup, archived.state)


def test_container_left_by_a_stopped_worker_is_made_anew(monkeypatch: pytest.MonkeyPatch) -> None:
    service, archived, fake = installed(CASES["tor"])
    archived.save(service, fake.host())
    clean = TarServiceHost(own_key="c", new_secret=OTHER_SECRET)
    put = ArchiveSeed.put

    def stopped(*args: object) -> None:
        raise SystemExit("the worker stops after docker run")

    monkeypatch.setattr(ArchiveSeed, "put", stopped)
    with pytest.raises(SystemExit):
        restore(service, archived, clean)
    monkeypatch.setattr(ArchiveSeed, "put", put)

    restore(service, archived, clean)  # the labelled container has keys of its own

    state = archived.state
    files = clean.container_files(state.container)
    assert files[state.identity_path] == fake.container_files(state.container)[state.identity_path]
    assert RESTORE_DONE in files


def test_card_warns_of_a_copy_too_large(client: Client) -> None:
    client.force_login(UserFactory(role=Role.ADMIN))
    service, archived, fake = installed(CASES["sftp"])
    archived.save(service, fake.host())
    service_copies.too_large(service, 300 * 1024 * 1024)

    html = client.get(f"/servers/{service.server_id}/").content.decode()

    assert "Copy of the files from" in html
    assert "300.0\xa0MB, more than a copy may hold" in html
    assert "The last copy stays." in html


def fernet_sealed(backup: ServiceBackup, data: bytes) -> None:
    """Seal the file whole by Fernet, as an older panel did."""
    key = Fernet.generate_key()
    default_storage.delete(str(backup.file.name))
    backup.file.name = default_storage.save(
        f"{FOLDER}/old.bin", ContentFile(Fernet(key).encrypt(data))
    )
    backup.file_key = key.decode()
    backup.save()


def test_new_copies_are_sealed_in_chunks() -> None:
    service, archived, fake = installed(CASES["mtproxy"])
    backup = archived.save(service, fake.host())
    assert backup is not None

    with default_storage.open(str(backup.file.name), "rb") as sealed:
        assert is_sealed(sealed.read(len(MAGIC)))
    assert is_stream_key(backup.file_key)


def test_a_fernet_copy_is_restored_and_replaced_by_the_next_read(
    media_root: Path, django_capture_on_commit_callbacks: Any
) -> None:
    service, archived, fake = installed(CASES["mtproxy"])
    backup = archived.save(service, fake.host())
    assert backup is not None
    data = service_copies.archive(backup, archived.state).data
    fernet_sealed(backup, data)

    assert service_copies.archive(backup, archived.state).data == data
    assert not service_copies.holds(service, backup.digest)

    with django_capture_on_commit_callbacks(execute=True):
        again = archived.save(service, fake.host())

    assert again is not None
    assert is_stream_key(again.file_key)
    assert again.digest == backup.digest
    assert service_copies.archive(again, archived.state).data == data
    assert [path.name for path in copy_files(media_root)] == [Path(str(again.file.name)).name]


def test_a_damaged_file_says_why() -> None:
    service, archived, fake = installed(CASES["mtproxy"])
    backup = archived.save(service, fake.host())
    assert backup is not None
    name = str(backup.file.name)
    with default_storage.open(name, "rb") as sealed:
        data = sealed.read()
    default_storage.delete(name)
    default_storage.save(name, ContentFile(data[:-1]))

    with pytest.raises(ArchiveFormatError, match="damaged"):
        service_copies.archive(backup, archived.state)


def tar_reads(monkeypatch: pytest.MonkeyPatch, fake: TarServiceHost) -> list[str]:
    """The tar scripts that read the files of the containers of `fake` from now on."""
    reads: list[str] = []
    answer = fake._tar

    def counted(name: str, script: str, stdin: bytes) -> CommandResult | None:
        if "tar -c" in script:
            reads.append(script)
        return answer(name, script, stdin)

    monkeypatch.setattr(fake, "_tar", counted)
    return reads


def test_after_a_change_the_files_are_read_once(
    monkeypatch: pytest.MonkeyPatch,
    media_root: Path,
    django_capture_on_commit_callbacks: Any,
) -> None:
    service, archived, fake = installed(CASES["sftp"])
    first = archived.save(service, fake.host())
    fake.put(SFTP_CONTAINER, UPLOAD, "report")
    reads = tar_reads(monkeypatch, fake)

    with django_capture_on_commit_callbacks(execute=True):
        second = archived.save(service, fake.host(), changed=True)

    assert len(reads) == 1
    assert first is not None
    assert second is not None
    assert second.digest != first.digest
    assert copy_files(media_root) == [media_root / str(second.file.name)]
    assert service_copies.archive(second, archived.state).files()[UPLOAD] == b"report"


def test_a_copy_of_the_same_files_keeps_the_saved_file(
    monkeypatch: pytest.MonkeyPatch, media_root: Path
) -> None:
    service, archived, fake = installed(CASES["sftp"])
    first = archived.save(service, fake.host())
    reads = tar_reads(monkeypatch, fake)

    second = archived.save(service, fake.host(), changed=True)

    assert len(reads) == 1
    assert first is not None
    assert second is not None
    assert second.file.name == first.file.name
    assert second.unchanged_since
    assert len(copy_files(media_root)) == 1


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_hash_of_a_copy_is_the_fingerprint_of_its_files(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, archived, fake = installed(CASES[case])
    first = archived.save(service, fake.host(), changed=True)
    reads = tar_reads(monkeypatch, fake)

    second = archived.save(service, fake.host())

    assert first is not None
    assert second is not None
    assert [read for read in reads if "tar -czf" in read] == []
    assert second.file.name == first.file.name


def test_a_service_change_reads_the_files_once(monkeypatch: pytest.MonkeyPatch) -> None:
    service, _archived, fake = installed(CASES["mtproxy"])
    reads = tar_reads(monkeypatch, fake)

    @contextmanager
    def connect(target: Server) -> Iterator[RemoteHost]:
        yield fake.host()

    monkeypatch.setattr(snapshot, "connect", connect)

    snapshot.save_service_copy(service.pk)

    assert len(reads) == 1
    assert ServiceBackup.objects.get(service=service).has_copy
