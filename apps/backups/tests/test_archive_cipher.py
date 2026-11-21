import io
import shutil
import subprocess
from pathlib import Path

import pytest

from apps.backups.services.archive_cipher import (
    HEADER,
    ArchiveCipher,
    ArchiveDecryptionError,
    DamagedArchiveError,
    WrongPasswordError,
)
from apps.backups.tests.panel_archive_helpers import decrypted, format_1

DATA = b"panel data " * 20_000
# The header, the password check, "Salted__" and the salt come before the ciphertext.
BODY_AT = len(HEADER) + 32 + 16


def encrypted(password: str, data: bytes = DATA) -> bytes:
    target = io.BytesIO()
    writer = ArchiveCipher(password).writer(target)
    for start in range(0, len(data), 7000):
        writer.write(data[start : start + 7000])
    writer.close()
    return target.getvalue()


def read_in_chunks(password: str, raw: bytes) -> bytes:
    reader = ArchiveCipher(password).reader(io.BytesIO(raw))
    chunks = []
    while chunk := reader.read(5000):
        chunks.append(chunk)
    return b"".join(chunks)


def flipped(raw: bytes, at: int) -> bytes:
    return raw[:at] + bytes([raw[at] ^ 1]) + raw[at + 1 :]


def test_the_archive_reads_back_with_its_password() -> None:
    raw = encrypted("correct horse ✓")

    assert raw.startswith(HEADER)
    assert raw[BODY_AT - 16 : BODY_AT - 8] == b"Salted__"
    assert DATA[:64] not in raw
    assert read_in_chunks("correct horse ✓", raw) == DATA


def test_a_wrong_password_is_told_from_damage() -> None:
    raw = encrypted("correct horse")

    with pytest.raises(WrongPasswordError):
        decrypted("wrong password", raw)
    with pytest.raises(WrongPasswordError):
        decrypted("wrong password", flipped(raw, BODY_AT + 100))


@pytest.mark.parametrize(
    "damage",
    [
        lambda raw: flipped(raw, BODY_AT + 100),
        lambda raw: flipped(raw, BODY_AT - 12),
        lambda raw: flipped(raw, len(raw) - 1),
        lambda raw: raw[:-5000],
        lambda raw: raw[:-1],
        lambda raw: raw + b"\0",
        lambda raw: raw[: BODY_AT + 10],
        lambda raw: raw[:40],
    ],
    ids=["ciphertext", "magic", "mac", "cut", "last byte", "extra byte", "no body", "no salt"],
)
def test_a_damaged_archive_is_reported_as_damaged(damage) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(DamagedArchiveError):
        decrypted("correct horse", damage(encrypted("correct horse")))


def test_damage_shows_before_any_byte_is_decrypted() -> None:
    raw = flipped(encrypted("correct horse"), len(HEADER) + 32 + 16 + 50_000)

    with pytest.raises(DamagedArchiveError):
        ArchiveCipher("correct horse").reader(io.BytesIO(raw))


def test_another_file_is_not_an_archive() -> None:
    with pytest.raises(DamagedArchiveError):
        decrypted("password", b"PK\x03\x04 a zip file")


def test_format_1_still_reads() -> None:
    raw = format_1("old password", DATA)

    assert read_in_chunks("old password", raw) == DATA
    assert not ArchiveCipher("old password").reader(io.BytesIO(raw)).authenticated


def test_format_1_cannot_tell_a_wrong_password(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the padding catches a wrong password: ~1 random salt in 256 slips by, this one does not.
    monkeypatch.setattr("apps.backups.tests.panel_archive_helpers.os.urandom", bytes)
    with pytest.raises(ArchiveDecryptionError) as error:
        decrypted("wrong password", format_1("correct horse", DATA))
    assert type(error.value) is ArchiveDecryptionError


@pytest.mark.skipif(shutil.which("openssl") is None, reason="no openssl")
def test_openssl_decrypts_the_archive(tmp_path: Path) -> None:
    """install.sh decrypts with openssl before any panel code runs on the new machine."""
    password = "пароль архива 1"
    (tmp_path / "password").write_text(password, encoding="utf-8")
    raw = encrypted(password, b"tar.gz bytes")[BODY_AT - 16 : -32]

    done = subprocess.run(
        [
            "openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "600000",
            "-md", "sha256", "-pass", f"file:{(tmp_path / 'password').as_posix()}",
        ],
        input=raw,
        capture_output=True,
        check=False,
    )  # fmt: skip

    assert done.returncode == 0, done.stderr
    assert done.stdout == b"tar.gz bytes"
