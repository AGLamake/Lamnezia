import io
import shutil
import subprocess
from pathlib import Path

import pytest

from apps.backups.services.archive_cipher import HEADER, ArchiveCipher, ArchiveDecryptionError

DATA = b"panel data " * 20_000


def encrypted(password: str, data: bytes = DATA) -> bytes:
    target = io.BytesIO()
    writer = ArchiveCipher(password).writer(target)
    for start in range(0, len(data), 7000):
        writer.write(data[start : start + 7000])
    writer.close()
    return target.getvalue()


def decrypted(password: str, raw: bytes) -> bytes:
    reader = ArchiveCipher(password).reader(io.BytesIO(raw))
    chunks = []
    while chunk := reader.read(5000):
        chunks.append(chunk)
    return b"".join(chunks)


def test_the_archive_reads_back_with_its_password() -> None:
    raw = encrypted("correct horse ✓")

    assert raw.startswith(HEADER + b"Salted__")
    assert DATA[:64] not in raw
    assert decrypted("correct horse ✓", raw) == DATA


def test_a_wrong_password_does_not_open_it(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the padding catches a wrong password: ~1 random salt in 256 slips by, this one does not.
    monkeypatch.setattr("apps.backups.services.archive_cipher.os.urandom", bytes)
    with pytest.raises(ArchiveDecryptionError):
        decrypted("wrong password", encrypted("correct horse"))


def test_another_file_is_not_an_archive() -> None:
    with pytest.raises(ArchiveDecryptionError):
        decrypted("password", b"PK\x03\x04 a zip file")


@pytest.mark.skipif(shutil.which("openssl") is None, reason="no openssl")
def test_openssl_decrypts_the_archive(tmp_path: Path) -> None:
    """install.sh decrypts with openssl before any panel code runs on the new machine."""
    password = "пароль архива 1"
    (tmp_path / "password").write_text(password, encoding="utf-8")
    raw = encrypted(password, b"tar.gz bytes")[len(HEADER) :]

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
