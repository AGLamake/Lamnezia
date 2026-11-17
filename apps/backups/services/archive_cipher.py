"""Password encryption of the panel archive, laid out so that `openssl enc -d` reads it too.

A file is the line ``LAMNEZIA-PANEL-ARCHIVE 1`` and then exactly what
``openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -md sha256`` writes: ``Salted__``, an 8-byte
salt and the AES-256-CBC ciphertext with PKCS#7 padding; key and IV come from PBKDF2-HMAC-SHA256
of the password. ``install.sh`` decrypts an archive with openssl before the panel exists.
"""

import hashlib
import os
from typing import BinaryIO

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, CipherContext, algorithms, modes

HEADER = b"LAMNEZIA-PANEL-ARCHIVE 1\n"
ITERATIONS = 600_000
_MAGIC = b"Salted__"
_SALT_SIZE = 8
_KEY_SIZE = 32
_BLOCK_BITS = 128
_CHUNK = 64 * 1024


class ArchiveDecryptionError(Exception):
    """The file is not a panel archive, the password is wrong or the file is damaged."""


class ArchiveCipher:
    """Turns a file object into an encrypting writer or a decrypting reader of the archive."""

    def __init__(self, password: str) -> None:
        if not password:
            raise ValueError("an archive password must not be empty")
        self._password = password.encode()

    def writer(self, target: BinaryIO) -> "EncryptingWriter":
        salt = os.urandom(_SALT_SIZE)
        target.write(HEADER + _MAGIC + salt)
        return EncryptingWriter(target, self._cipher(salt).encryptor())

    def reader(self, source: BinaryIO) -> "DecryptingReader":
        start = source.read(len(HEADER) + len(_MAGIC) + _SALT_SIZE)
        if not start.startswith(HEADER + _MAGIC) or len(start) < len(HEADER) + 16:
            raise ArchiveDecryptionError("not a panel archive")
        return DecryptingReader(source, self._cipher(start[-_SALT_SIZE:]).decryptor())

    def _cipher(self, salt: bytes) -> Cipher[modes.CBC]:
        material = hashlib.pbkdf2_hmac(
            "sha256", self._password, salt, ITERATIONS, _KEY_SIZE + _BLOCK_BITS // 8
        )
        return Cipher(algorithms.AES(material[:_KEY_SIZE]), modes.CBC(material[_KEY_SIZE:]))


class EncryptingWriter:
    """A write-only stream: what is written lands encrypted; `close` adds the padding."""

    def __init__(self, target: BinaryIO, encryptor: CipherContext) -> None:
        self._target = target
        self._encryptor = encryptor
        self._padder = padding.PKCS7(_BLOCK_BITS).padder()

    def write(self, data: bytes) -> int:
        self._target.write(self._encryptor.update(self._padder.update(data)))
        return len(data)

    def close(self) -> None:
        tail = self._padder.finalize()
        self._target.write(self._encryptor.update(tail) + self._encryptor.finalize())


class DecryptingReader:
    """A read-only stream of the decrypted archive; a wrong password fails at the end at last."""

    def __init__(self, source: BinaryIO, decryptor: CipherContext) -> None:
        self._source = source
        self._decryptor = decryptor
        self._unpadder = padding.PKCS7(_BLOCK_BITS).unpadder()
        self._buffer = b""
        self._done = False

    def read(self, size: int = -1) -> bytes:
        while not self._done and (size < 0 or len(self._buffer) < size):
            self._buffer += self._next_chunk()
        if size < 0:
            size = len(self._buffer)
        data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

    def _next_chunk(self) -> bytes:
        chunk = self._source.read(_CHUNK)
        if chunk:
            return self._unpadder.update(self._decryptor.update(chunk))
        self._done = True
        try:
            tail = self._decryptor.finalize()
            return self._unpadder.update(tail) + self._unpadder.finalize()
        except ValueError as exc:
            raise ArchiveDecryptionError("wrong password or damaged archive") from exc
