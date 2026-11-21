"""Streaming encryption of large files in authenticated chunks: AES-256-GCM, STREAM construction.

A file is a header and chunks of at most `CHUNK_SIZE` bytes, each sealed on its own, so sealing
and opening hold one chunk in memory whatever the size of the file. The nonce of a chunk is a
random prefix of the file, the number of the chunk and a mark of the last one, and the header is
authenticated with every chunk: a damaged, reordered, cut or extended file is refused with a
reason instead of yielding wrong data.

Header: `MAGIC`, the chunk size (4 bytes, big-endian) and the nonce prefix (7 bytes).
"""

import base64
import os
import struct
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"LMZS\x01"
CHUNK_SIZE = 64 * 1024
# A header asking for larger chunks is not one this panel made: refused before any allocation.
MAX_CHUNK_SIZE = 4 * 1024 * 1024
KEY_SIZE = 32
TAG_SIZE = 16
PREFIX_SIZE = 7
HEADER = struct.Struct(f">{len(MAGIC)}sI{PREFIX_SIZE}s")
# Keys of this format carry the prefix: a key without it is a Fernet key of an older panel.
KEY_PREFIX = "aes256gcm-stream:"
_MAX_CHUNKS = 2**32


class Readable(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


class Writable(Protocol):
    def write(self, data: bytes, /) -> object: ...


class SealedFileError(ValueError):
    """The file is not sealed by this format or was changed after sealing."""


def generate_key() -> str:
    """A new random key, as the text kept encrypted in the database."""
    return KEY_PREFIX + base64.urlsafe_b64encode(AESGCM.generate_key(bit_length=256)).decode()


def is_stream_key(key: str) -> bool:
    return key.startswith(KEY_PREFIX)


def is_sealed(head: bytes) -> bool:
    """Whether the first bytes of a file are those of this format."""
    return head.startswith(MAGIC)


class StreamCipher:
    """Seals a stream into chunks and opens it back, writing as it goes."""

    def __init__(self, key: str) -> None:
        if not is_stream_key(key):
            raise SealedFileError("the key is not one of the chunked format")
        raw = base64.urlsafe_b64decode(key.removeprefix(KEY_PREFIX))
        if len(raw) != KEY_SIZE:
            raise SealedFileError("the key is not 256 bits long")
        self._aead = AESGCM(raw)

    def seal(self, source: Readable, target: Writable, chunk_size: int = CHUNK_SIZE) -> None:
        header = HEADER.pack(MAGIC, chunk_size, os.urandom(PREFIX_SIZE))
        target.write(header)
        chunks = _Chunks(header)
        current = _read_exactly(source, chunk_size)
        while True:
            following = _read_exactly(source, chunk_size)
            last = not following
            target.write(self._aead.encrypt(chunks.nonce(last), current, header))
            if last:
                return
            current = following

    def open(self, source: Readable, target: Writable) -> None:
        header = _read_exactly(source, HEADER.size)
        if len(header) < HEADER.size or not is_sealed(header):
            raise SealedFileError("the file is not a sealed copy")
        _, chunk_size, _ = HEADER.unpack(header)
        if not 0 < chunk_size <= MAX_CHUNK_SIZE:
            raise SealedFileError("the header of the file is damaged")
        chunks = _Chunks(header)
        size = chunk_size + TAG_SIZE
        current = _read_exactly(source, size)
        while True:
            following = _read_exactly(source, size)
            last = not following
            target.write(self._opened(chunks, current, header, last=last))
            if last:
                return
            current = following

    def _opened(self, chunks: "_Chunks", sealed: bytes, header: bytes, *, last: bool) -> bytes:
        if not sealed:
            raise SealedFileError("the file is cut short")
        nonce = chunks.nonce(last)
        try:
            return self._aead.decrypt(nonce, sealed, header)
        except InvalidTag:
            pass
        if last and self._opens_as_middle(nonce, sealed, header):
            raise SealedFileError("the file is cut short")
        raise SealedFileError("the file is damaged or its chunks are out of order")

    def _opens_as_middle(self, nonce: bytes, sealed: bytes, header: bytes) -> bool:
        try:
            self._aead.decrypt(nonce[:-1] + b"\x00", sealed, header)
        except InvalidTag:
            return False
        return True


class _Chunks:
    """The nonces of the chunks of one file, in order."""

    def __init__(self, header: bytes) -> None:
        self._prefix = header[-PREFIX_SIZE:]
        self._number = 0

    def nonce(self, last: bool) -> bytes:
        if self._number >= _MAX_CHUNKS:
            raise SealedFileError("the file has too many chunks")
        nonce = self._prefix + struct.pack(">I?", self._number, last)
        self._number += 1
        return nonce


def _read_exactly(source: Readable, size: int) -> bytes:
    """Up to `size` bytes; fewer only at the end of the stream."""
    parts: list[bytes] = []
    left = size
    while left:
        part = source.read(left)
        if not part:
            break
        parts.append(part)
        left -= len(part)
    return b"".join(parts)
