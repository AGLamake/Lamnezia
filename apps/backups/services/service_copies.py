"""Copies of the service containers on the panel disk, each file encrypted under a key of its own.

A copy of the Tor site or of the SFTP uploads may be hundreds of megabytes: it is a file in the
media folder, which the panel archive and the backup service take along, not a value in the
database. The key of the file is kept encrypted in its row (`ServiceBackup.file_key`): the file
alone tells nothing, and rotating LAMNEZIA_ENCRYPTION_KEY re-encrypts the key with the rest.
The copy comes from the server as a temporary file and is sealed and opened in chunks
(`sealed_files`), from file to file: memory holds a chunk, whatever the size of the copy. A
file sealed whole by Fernet, as older panels did, is still read, and the next read of the
service replaces it.
"""

import tempfile
from contextlib import ExitStack
from functools import partial
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from django.core.files import File
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from amnezia_core.container_archive import ArchiveFormatError, ContainerArchive, ContainerState
from apps.backups.models import ServiceBackup
from apps.backups.services.sealed_files import (
    SealedFileError,
    StreamCipher,
    Writable,
    generate_key,
    is_stream_key,
)
from apps.protocols.models import ServiceInstance

FOLDER = "service-backups"


class ServiceCopies:
    """Keeps, reads and forgets the copies of the services, and notes what each read found.

    Two reads of one service may end at once (the hourly copy and one asked for after a
    change): the row is locked while it changes, and the file a copy replaces is deleted
    once the new one is saved.
    """

    def save(self, service: ServiceInstance, copy: ContainerArchive, digest: str) -> ServiceBackup:
        """Keep a new copy of the files whose fingerprint is `digest`."""
        key = generate_key()
        with tempfile.TemporaryFile() as sealed:
            with copy.stream() as tar_gz:
                StreamCipher(key).seal(tar_gz, sealed)
            sealed.seek(0)
            name = default_storage.save(f"{FOLDER}/{uuid4().hex}.bin", File(sealed))
        try:
            with transaction.atomic():
                backup = self._locked(service)
                replaced = str(backup.file.name or "")
                now = timezone.now()
                backup.file.name = name
                backup.file_key = key
                backup.archive = ""
                backup.digest = digest
                backup.taken_at = backup.checked_at = now
                backup.too_large_at = backup.too_large_size = None
                backup.save()
                if replaced:
                    transaction.on_commit(partial(forget_file, replaced))
        except BaseException:
            forget_file(name)
            raise
        return backup

    def holds(self, service: ServiceInstance, digest: str) -> bool:
        """Whether the saved copy is of the files with this fingerprint and sealed in chunks."""
        copies = ServiceBackup.objects.filter(service=service, digest=digest).exclude(file="")
        backup = copies.only("file_key").first() if digest else None
        return backup is not None and is_stream_key(backup.file_key)

    def checked(self, service: ServiceInstance) -> ServiceBackup:
        """The files were read again and are those of the copy; the copy stays."""
        with transaction.atomic():
            backup = self._locked(service)
            backup.checked_at = timezone.now()
            backup.too_large_at = backup.too_large_size = None
            backup.save(update_fields=["checked_at", "too_large_at", "too_large_size"])
        return backup

    def too_large(self, service: ServiceInstance, size: int) -> ServiceBackup:
        """The files are more than the copy may hold: none is taken, the last one stays."""
        with transaction.atomic():
            backup = self._locked(service)
            backup.too_large_at = timezone.now()
            backup.too_large_size = size
            backup.save(update_fields=["too_large_at", "too_large_size"])
        return backup

    def archive(self, backup: ServiceBackup, state: ContainerState) -> ContainerArchive:
        """The saved copy, checked, in a temporary file it owns; ArchiveFormatError if bad."""
        if not backup.file:
            return ContainerArchive.decode(state, backup.archive)  # saved by an older panel
        with ExitStack() as unless_returned:
            opened = unless_returned.enter_context(tempfile.TemporaryFile())
            self._open(backup, opened)
            archive = ContainerArchive(state, opened)
            unless_returned.pop_all()
        return archive

    @staticmethod
    def _open(backup: ServiceBackup, target: Writable) -> None:
        """Decrypt the file of the copy into `target`."""
        try:
            with default_storage.open(str(backup.file.name), "rb") as sealed:
                if not is_stream_key(backup.file_key):  # sealed whole by an older panel
                    target.write(Fernet(backup.file_key.encode()).decrypt(sealed.read()))
                else:
                    StreamCipher(backup.file_key).open(sealed, target)
        except SealedFileError as error:
            raise ArchiveFormatError(f"the file of the copy cannot be read: {error}") from error
        except (OSError, InvalidToken, ValueError) as error:
            raise ArchiveFormatError("the file of the copy cannot be read") from error

    @staticmethod
    def _locked(service: ServiceInstance) -> ServiceBackup:
        ServiceBackup.objects.get_or_create(service=service)
        return ServiceBackup.objects.select_for_update().get(service=service)


def forget_file(name: str) -> None:
    """Delete a copy file; one already gone is no error."""
    default_storage.delete(name)


service_copies = ServiceCopies()
