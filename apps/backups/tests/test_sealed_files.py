"""Chunked sealing: round trips, memory that does not grow with the file, refused changes."""

import io
import os
import tracemalloc
from collections.abc import Callable
from pathlib import Path

import pytest

from apps.backups.services.sealed_files import (
    CHUNK_SIZE,
    HEADER,
    KEY_PREFIX,
    TAG_SIZE,
    SealedFileError,
    StreamCipher,
    generate_key,
)

KEY = generate_key()
SMALL = 16
SEALED_CHUNK = SMALL + TAG_SIZE


def sealed(data: bytes, chunk_size: int = SMALL, key: str = KEY) -> bytes:
    target = io.BytesIO()
    StreamCipher(key).seal(io.BytesIO(data), target, chunk_size)
    return target.getvalue()


def opened(data: bytes, key: str = KEY) -> bytes:
    target = io.BytesIO()
    StreamCipher(key).open(io.BytesIO(data), target)
    return target.getvalue()


def refused(data: bytes, key: str = KEY) -> str:
    with pytest.raises(SealedFileError) as error:
        opened(data, key)
    return str(error.value)


def chunks(data: bytes) -> tuple[bytes, list[bytes]]:
    body = data[HEADER.size :]
    return data[: HEADER.size], [
        body[i : i + SEALED_CHUNK] for i in range(0, len(body), SEALED_CHUNK)
    ]


@pytest.mark.parametrize("size", [0, 1, SMALL - 1, SMALL, SMALL + 1, 3 * SMALL, 5 * SMALL + 7])
def test_round_trip(size: int) -> None:
    data = os.urandom(size)

    assert opened(sealed(data)) == data


def test_the_same_data_seals_differently_and_hides_it() -> None:
    data = b"onion key " * 20

    first, second = sealed(data), sealed(data)

    assert first != second
    assert b"onion key" not in first


def test_another_key_does_not_open_it() -> None:
    assert "damaged" in refused(sealed(b"secret" * 10), generate_key())


def test_a_flipped_byte_is_refused() -> None:
    data = bytearray(sealed(os.urandom(3 * SMALL)))
    data[HEADER.size + SEALED_CHUNK + 3] ^= 1

    assert "damaged" in refused(bytes(data))


def test_a_changed_header_is_refused() -> None:
    data = bytearray(sealed(os.urandom(3 * SMALL)))
    data[HEADER.size - 1] ^= 1  # the last byte of the nonce prefix

    assert "damaged" in refused(bytes(data))


def test_swapped_chunks_are_refused() -> None:
    header, parts = chunks(sealed(os.urandom(4 * SMALL)))
    parts[0], parts[1] = parts[1], parts[0]

    assert "out of order" in refused(header + b"".join(parts))


def test_a_file_cut_at_a_chunk_is_refused() -> None:
    header, parts = chunks(sealed(os.urandom(4 * SMALL)))

    assert refused(header + b"".join(parts[:2])) == "the file is cut short"
    assert refused(header) == "the file is cut short"


def test_a_file_cut_inside_a_chunk_is_refused() -> None:
    data = sealed(os.urandom(4 * SMALL))

    assert "damaged" in refused(data[:-5])


def test_bytes_after_the_last_chunk_are_refused() -> None:
    header, parts = chunks(sealed(os.urandom(2 * SMALL)))

    assert "damaged" in refused(header + b"".join(parts) + parts[-1])


def test_a_file_of_another_format_is_refused() -> None:
    assert refused(b"gAAAAABfernet-token") == "the file is not a sealed copy"


def test_a_header_asking_for_huge_chunks_is_refused() -> None:
    data = bytearray(sealed(b"x"))
    data[5:9] = (2**31).to_bytes(4, "big")

    assert refused(bytes(data)) == "the header of the file is damaged"


def test_a_key_of_another_format_is_refused() -> None:
    with pytest.raises(SealedFileError):
        StreamCipher("ZmVybmV0LWtleQ==")
    with pytest.raises(SealedFileError):
        StreamCipher(KEY_PREFIX + "c2hvcnQ=")


class Pattern(io.RawIOBase):
    """`size` bytes made on the fly, so the test holds no copy of them."""

    def __init__(self, size: int) -> None:
        self._left = size

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: memoryview) -> int:  # type: ignore[override]
        count = min(len(buffer), self._left)
        buffer[:count] = b"\xa5" * count
        self._left -= count
        return count


class Sink(io.RawIOBase):
    """Counts what is written and keeps nothing."""

    def __init__(self) -> None:
        self.size = 0
        self.wrong = False

    def writable(self) -> bool:
        return True

    def write(self, data: bytes) -> int:  # type: ignore[override]
        self.wrong |= data.strip(b"\xa5") != b""
        self.size += len(data)
        return len(data)


def peak_memory(action: Callable[[], None]) -> int:
    tracemalloc.start()
    try:
        action()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_memory_does_not_grow_with_the_file(tmp_path: Path) -> None:
    size = 48 * 1024 * 1024
    path = tmp_path / "copy.bin"
    cipher = StreamCipher(KEY)

    def seal() -> None:
        with path.open("wb") as target:
            cipher.seal(io.BufferedReader(Pattern(size)), target)

    sink = Sink()

    def open_() -> None:
        with path.open("rb") as source:
            cipher.open(source, sink)

    limit = 8 * CHUNK_SIZE
    assert peak_memory(seal) < limit
    assert peak_memory(open_) < limit
    assert sink.size == size
    assert not sink.wrong
