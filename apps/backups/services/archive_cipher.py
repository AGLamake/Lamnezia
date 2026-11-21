"""Password encryption of the panel archive, laid out so that openssl and coreutils read it too.

Format 2, written now: the line ``LAMNEZIA-PANEL-ARCHIVE 2``, a 32-byte password check, then
exactly what ``openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -md sha256`` writes (``Salted__``,
an 8-byte salt, the AES-256-CBC ciphertext with PKCS#7 padding), and at the end a 32-byte
HMAC-SHA256 of everything before it. Key and IV are PBKDF2-HMAC-SHA256 of the password, as openssl
derives them; the check is SHA-256(key ‖ iv ‖ "check"), the MAC key SHA-256(key ‖ iv ‖ "mac"). A
wrong password shows at the check, a damaged file at the MAC — both before a byte is decrypted.

Format 1, still read: the line ``LAMNEZIA-PANEL-ARCHIVE 1`` and the openssl output, with neither
check nor MAC, so a wrong password and a damaged file look the same.

``install.sh`` decrypts an archive before the panel exists, with only openssl and coreutils on the
machine. ``openssl enc`` refuses AEAD ciphers such as AES-GCM, hence encrypt-then-MAC of the
primitives it has.
"""

import hashlib
import hmac
import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import BinaryIO

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, CipherContext, algorithms, modes

HEADER = b"LAMNEZIA-PANEL-ARCHIVE 2\n"
HEADER_V1 = b"LAMNEZIA-PANEL-ARCHIVE 1\n"
ITERATIONS = 600_000
# The archive may leave the server: the password is all that guards the secrets in it.
MIN_PASSWORD_LENGTH = 12
_MAGIC = b"Salted__"
_SALT_SIZE = 8
_KEY_SIZE = 32
_DIGEST_SIZE = 32
_BLOCK_BITS = 128
_CHUNK = 64 * 1024


class ArchiveDecryptionError(Exception):
    """The archive does not open: a wrong password or a damaged file, format 1 cannot tell which."""


class WrongPasswordError(ArchiveDecryptionError):
    """The password check of the archive differs: the file is fine, the password is not."""


class DamagedArchiveError(ArchiveDecryptionError):
    """The file is damaged, incomplete or not a panel archive."""


@dataclass(frozen=True)
class _Keys:
    """What the password and the salt of one archive give."""

    key: bytes
    iv: bytes

    @classmethod
    def derive(cls, password: bytes, salt: bytes) -> "_Keys":
        material = hashlib.pbkdf2_hmac(
            "sha256", password, salt, ITERATIONS, _KEY_SIZE + _BLOCK_BITS // 8
        )
        return cls(material[:_KEY_SIZE], material[_KEY_SIZE:])

    def cipher(self) -> Cipher[modes.CBC]:
        return Cipher(algorithms.AES(self.key), modes.CBC(self.iv))

    def check(self) -> bytes:
        return hashlib.sha256(self.key + self.iv + b"check").digest()

    def mac(self) -> "hmac.HMAC":
        return hmac.new(hashlib.sha256(self.key + self.iv + b"mac").digest(), digestmod="sha256")


class ArchiveCipher:
    """Turns a file object into an encrypting writer or a decrypting reader of the archive."""

    def __init__(self, password: str) -> None:
        if not password:
            raise ValueError("an archive password must not be empty")
        self._password = password.encode()

    def writer(self, target: BinaryIO) -> "EncryptingWriter":
        salt = os.urandom(_SALT_SIZE)
        keys = _Keys.derive(self._password, salt)
        start = HEADER + keys.check() + _MAGIC + salt
        mac = keys.mac()
        mac.update(start)
        target.write(start)
        return EncryptingWriter(target, keys.cipher().encryptor(), mac)

    def reader(self, source: BinaryIO) -> "DecryptingReader":
        """A reader of either format; format 2 is checked whole first, so `source` must seek."""
        header = source.read(len(HEADER))
        if header == HEADER:
            return self._authenticated_reader(source)
        if header == HEADER_V1:
            start = source.read(len(_MAGIC) + _SALT_SIZE)
            if len(start) < len(_MAGIC) + _SALT_SIZE or not start.startswith(_MAGIC):
                raise DamagedArchiveError("no salt")
            decryptor = _Keys.derive(self._password, start[-_SALT_SIZE:]).cipher().decryptor()
            return DecryptingReader(source, decryptor)
        raise DamagedArchiveError("not a panel archive")

    def _authenticated_reader(self, source: BinaryIO) -> "DecryptingReader":
        start = source.read(_DIGEST_SIZE + len(_MAGIC) + _SALT_SIZE)
        if len(start) < _DIGEST_SIZE + len(_MAGIC) + _SALT_SIZE or (
            start[_DIGEST_SIZE : _DIGEST_SIZE + len(_MAGIC)] != _MAGIC
        ):
            raise DamagedArchiveError("short header")
        keys = _Keys.derive(self._password, start[-_SALT_SIZE:])
        if not hmac.compare_digest(start[:_DIGEST_SIZE], keys.check()):
            raise WrongPasswordError("password check differs")
        body_at = source.tell()
        size = source.seek(0, os.SEEK_END) - body_at - _DIGEST_SIZE
        if size <= 0:
            raise DamagedArchiveError("no ciphertext")
        source.seek(body_at)
        mac = keys.mac()
        mac.update(HEADER + start)
        for chunk in _chunks(source, size):
            mac.update(chunk)
        if not hmac.compare_digest(source.read(_DIGEST_SIZE), mac.digest()):
            raise DamagedArchiveError("MAC differs")
        source.seek(body_at)
        return DecryptingReader(source, keys.cipher().decryptor(), size)


class EncryptingWriter:
    """A write-only stream that encrypts what is written; `close` adds the padding and the MAC."""

    def __init__(self, target: BinaryIO, encryptor: CipherContext, mac: "hmac.HMAC") -> None:
        self._target = target
        self._encryptor = encryptor
        self._mac = mac
        self._padder = padding.PKCS7(_BLOCK_BITS).padder()

    def write(self, data: bytes) -> int:
        self._emit(self._encryptor.update(self._padder.update(data)))
        return len(data)

    def close(self) -> None:
        tail = self._padder.finalize()
        self._emit(self._encryptor.update(tail) + self._encryptor.finalize())
        self._target.write(self._mac.digest())

    def _emit(self, ciphertext: bytes) -> None:
        self._mac.update(ciphertext)
        self._target.write(ciphertext)


class DecryptingReader:
    """A read-only stream of the decrypted archive.

    `size` bytes of ciphertext are read, or all of the source when it is None (format 1, where a
    wrong password fails at the very end at last).
    """

    def __init__(self, source: BinaryIO, decryptor: CipherContext, size: int | None = None) -> None:
        self.authenticated = size is not None
        self._chunks = _chunks(source, size)
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
        chunk = next(self._chunks, b"")
        if chunk:
            return self._unpadder.update(self._decryptor.update(chunk))
        self._done = True
        try:
            tail = self._decryptor.finalize()
            return self._unpadder.update(tail) + self._unpadder.finalize()
        except ValueError as exc:
            if self.authenticated:
                raise DamagedArchiveError("bad padding") from exc
            raise ArchiveDecryptionError("wrong password or damaged archive") from exc


def _chunks(source: BinaryIO, size: int | None) -> Iterator[bytes]:
    """Chunks of `source`: `size` bytes of it, or all when None; a short file is damaged."""
    left = size
    while left is None or left > 0:
        chunk = source.read(_CHUNK if left is None else min(_CHUNK, left))
        if not chunk:
            if left:
                raise DamagedArchiveError("file ends early")
            return
        if left is not None:
            left -= len(chunk)
        yield chunk
