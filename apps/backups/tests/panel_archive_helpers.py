"""Test helpers of the panel archive: a stand-in for pg_dump, the folder, format 1 archives."""

import hashlib
import io
import os
from pathlib import Path

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.conf import settings

from apps.backups.services.archive_cipher import HEADER_V1, ITERATIONS, ArchiveCipher
from apps.backups.services.panel_archives import PanelArchiveFolder

DUMP = b"PGDMP fake custom dump"
PASSWORD = "archive password 1"


class FakeDump:
    def __init__(self, content: bytes = DUMP) -> None:
        self.content = content

    def write(self, path: Path) -> None:
        path.write_bytes(self.content)


def folder(keep: int = 3) -> PanelArchiveFolder:
    return PanelArchiveFolder(settings.PANEL_ARCHIVE_DIR, keep)


def format_1(password: str, data: bytes) -> bytes:
    """An archive as panels before format 2 wrote it: the header and bare openssl enc output."""
    salt = os.urandom(8)
    material = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS, 48)
    encryptor = Cipher(algorithms.AES(material[:32]), modes.CBC(material[32:])).encryptor()
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    return HEADER_V1 + b"Salted__" + salt + encryptor.update(padded) + encryptor.finalize()


def downgrade(path: Path, password: str) -> None:
    """Rewrite a format 2 archive file in format 1, the content unchanged."""
    with path.open("rb") as raw:
        content = ArchiveCipher(password).reader(raw).read()
    path.write_bytes(format_1(password, content))


def decrypted(password: str, raw: bytes) -> bytes:
    return ArchiveCipher(password).reader(io.BytesIO(raw)).read()
