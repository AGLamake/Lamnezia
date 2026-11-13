"""The site of Tor and the SFTP uploads in the copy, an unfinished restore, a fresh service copy."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

import pytest
from django.core.cache import cache

from amnezia_core.container_archive import (
    MAX_ARCHIVE_SIZE,
    RESTORE_MARKER,
    SFTP_STATE,
    TOR_DATABASE_DUMP,
    TOR_STATE,
    ArchiveFormatError,
    ContainerArchive,
)
from amnezia_core.proxies import SECRET_PATH
from amnezia_core.services import SFTP_CONTAINER, TOR_CONTAINER
from apps.backups import tasks
from apps.backups.models import ServiceBackup
from apps.backups.services import snapshot
from apps.backups.services.freshness import take_fresh_service_copy
from apps.backups.tests.fake_containers import SITE_DATABASE, TarServiceHost, pack
from apps.backups.tests.test_services import CASES, OTHER_SECRET, installed, restore
from apps.protocols.models import ServiceInstance
from apps.protocols.signals import service_changed
from apps.servers.models import Server
from apps.ssh.errors import SshCommandError
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db

UPLOAD = "/var/www/html/wp-content/uploads/2026/11/cat.jpg"
SFTP_UPLOAD = "/home/sftp_user/upload/report.pdf"


@pytest.fixture
def scheduled(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[Any, ...]]]:
    """The fresh service copies asked for, as (service id, countdown)."""
    cache.clear()
    calls: list[tuple[Any, ...]] = []

    def apply_async(args: tuple[int], countdown: int) -> None:
        calls.append((*args, countdown))

    monkeypatch.setattr(tasks.save_service_backup, "apply_async", apply_async)
    yield calls
    cache.clear()


def test_tor_copy_holds_the_site_and_a_dump_of_its_database() -> None:
    command = TOR_STATE.archive_command()

    assert command.startswith(f"mkdir -p /var/lib/lamnezia && {TOR_DATABASE_DUMP.dump} > ")
    assert "--ignore-failed-read" in command
    for folder in ("var/www/html/wp-content", "var/www/html/wp-config.php", "var/lib/tor"):
        assert folder in command
    assert command.endswith(f"status=$?; rm -f {TOR_DATABASE_DUMP.path}; exit $status")
    assert "home" in SFTP_STATE.archive_command()


def test_copies_with_user_files_may_be_larger() -> None:
    big = b"x" * (MAX_ARCHIVE_SIZE + 1)
    data = pack({SFTP_UPLOAD: big}, SFTP_STATE.archive_command()).stdout
    files = ContainerArchive(SFTP_STATE, data).files()

    assert files[SFTP_UPLOAD] == big
    with pytest.raises(ArchiveFormatError, match="too large"):
        ContainerArchive(replace(SFTP_STATE, max_size=MAX_ARCHIVE_SIZE), data)


def test_restore_brings_back_the_tor_site() -> None:
    service, archived, fake = installed(CASES["tor"])
    fake.put(TOR_CONTAINER, UPLOAD, "cat")
    fake.put(TOR_CONTAINER, SITE_DATABASE, "INSERT INTO wp_posts ...")
    archived.save(service, fake.host())

    clean = TarServiceHost(own_key="c", new_secret=OTHER_SECRET)
    restore(service, archived, clean)

    files = clean.container_files(TOR_CONTAINER)
    assert files[UPLOAD] == b"cat"
    assert files[SITE_DATABASE] == b"INSERT INTO wp_posts ..."
    assert TOR_DATABASE_DUMP.path not in files
    assert RESTORE_MARKER not in files


def test_restore_brings_back_the_sftp_uploads() -> None:
    service, archived, fake = installed(CASES["sftp"])
    fake.put(SFTP_CONTAINER, SFTP_UPLOAD, "report")
    archived.save(service, fake.host())

    clean = TarServiceHost(own_key="c", new_secret=OTHER_SECRET)
    restore(service, archived, clean)

    assert clean.container_files(SFTP_CONTAINER)[SFTP_UPLOAD] == b"report"


def test_container_of_an_unfinished_restore_is_made_anew() -> None:
    service, archived, fake = installed(CASES["tor"])
    archived.save(service, fake.host())
    clean = TarServiceHost(own_key="c", new_secret=OTHER_SECRET, failing_extracts=1)
    with pytest.raises(SshCommandError):
        restore(service, archived, clean)
    assert RESTORE_MARKER in clean.container_files(TOR_CONTAINER)

    restore(service, archived, clean)  # the container has keys of its own: it is not refused

    files = clean.container_files(TOR_CONTAINER)
    assert (
        files[TOR_STATE.identity_path]
        == fake.container_files(TOR_CONTAINER)[TOR_STATE.identity_path]
    )
    assert RESTORE_MARKER not in files


def test_changed_service_asks_for_a_copy_at_once(scheduled: list[tuple[Any, ...]]) -> None:
    service, _archived, _fake = installed(CASES["mtproxy"])

    service_changed.send(object, service=service)
    service_changed.send(object, service=service)

    assert scheduled == [(service.pk, 0)]


def test_service_without_a_copy_asks_for_nothing(scheduled: list[tuple[Any, ...]]) -> None:
    service = ServiceInstance(pk=99, container="amnezia-socks5proxy")

    service_changed.send(object, service=service)

    assert scheduled == []


def test_fresh_copy_holds_the_new_secret(
    monkeypatch: pytest.MonkeyPatch, scheduled: list[tuple[Any, ...]]
) -> None:
    service, archived, fake = installed(CASES["mtproxy"])
    archived.save(service, fake.host())
    fake.put(service.container, SECRET_PATH, f"{OTHER_SECRET}\n")
    service.secret = OTHER_SECRET
    service.save(update_fields=["secret"])

    @contextmanager
    def connect(target: Server) -> Iterator[RemoteHost]:
        yield fake.host()

    monkeypatch.setattr(snapshot, "connect", connect)
    take_fresh_service_copy(service.pk)

    backup = ServiceBackup.objects.get(service=service)
    assert ContainerArchive.decode(archived.state, backup.archive).identity() == OTHER_SECRET
