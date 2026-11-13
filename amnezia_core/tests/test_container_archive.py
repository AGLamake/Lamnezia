import base64
import io
import tarfile
from collections.abc import Callable

import pytest
from cryptography.hazmat.primitives import serialization

from amnezia_core import ipsec, openvpn
from amnezia_core.container_archive import (
    AWG_LEGACY_STATE,
    IPSEC_STATE,
    OPENVPN_STATE,
    WIREGUARD_STATE,
    XRAY_STATE,
    ArchiveFormatError,
    ContainerArchive,
)
from amnezia_core.tests.pki import FakeCa


def tar_gz(files: dict[str, bytes], *, symlink: str | None = None) -> bytes:
    """A tar.gz with paths relative to /, as `tar -czf - -C /` writes it."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, content in files.items():
            info = tarfile.TarInfo(path.lstrip("/"))
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        if symlink:
            link = tarfile.TarInfo(symlink.lstrip("/"))
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/shadow"
            archive.addfile(link)
    return buffer.getvalue()


def test_archive_command_names_folders_relative_to_root() -> None:
    assert IPSEC_STATE.archive_command() == "tar -czf - -C / etc/ipsec.d opt/amnezia/ikev2"


def test_wireguard_identity_is_private_key(read_fixture: Callable[[str], str]) -> None:
    config = read_fixture("wireguard_wg0.conf").encode()
    archive = ContainerArchive(WIREGUARD_STATE, tar_gz({WIREGUARD_STATE.identity_path: config}))

    assert archive.identity() == "+I1o14tu7eCcREXj1rrqH+CzICpdRvfQoTHi5vo+v0s="


def test_awg_legacy_and_xray_identity(read_fixture: Callable[[str], str]) -> None:
    legacy = read_fixture("awg_legacy_wg0.conf").encode()
    server_json = read_fixture("xray_server.json").encode()

    awg = ContainerArchive(AWG_LEGACY_STATE, tar_gz({AWG_LEGACY_STATE.identity_path: legacy}))
    xray = ContainerArchive(XRAY_STATE, tar_gz({XRAY_STATE.identity_path: server_json}))

    assert awg.identity() == "+I1o14tu7eCcREXj1rrqH+CzICpdRvfQoTHi5vo+v0s="
    assert xray.identity() == "uGQ5Zl0n6xkVYPWk5aM4lJx2bS7cT8dE9fG0hI1jK2M"


def test_openvpn_and_ipsec_identity_is_ca_fingerprint() -> None:
    ca = FakeCa()
    der = ca.certificate.public_bytes(serialization.Encoding.DER)
    p12 = base64.b64encode(der)

    vpn = ContainerArchive(OPENVPN_STATE, tar_gz({OPENVPN_STATE.identity_path: ca.pem.encode()}))
    ike = ContainerArchive(IPSEC_STATE, tar_gz({IPSEC_STATE.identity_path: p12}))

    assert vpn.identity() == openvpn.ca_fingerprint(ca.pem)
    assert ike.identity() == ipsec.ca_fingerprint(p12.decode())


def test_round_trip_through_base64() -> None:
    data = tar_gz({"/opt/amnezia/openvpn/pki/index.txt": b"V\t300101000000Z\t\t01\tunknown\n"})
    archive = ContainerArchive(OPENVPN_STATE, data)

    restored = ContainerArchive.decode(OPENVPN_STATE, archive.encode())

    assert restored.files() == {
        "/opt/amnezia/openvpn/pki/index.txt": archive.files()["/opt/amnezia/openvpn/pki/index.txt"]
    }


def test_missing_identity_file_is_refused() -> None:
    archive = ContainerArchive(OPENVPN_STATE, tar_gz({"/opt/amnezia/openvpn/server.conf": b""}))

    with pytest.raises(ArchiveFormatError):
        archive.identity()


@pytest.mark.parametrize(
    "files",
    [
        {"/etc/passwd": b"root"},
        {"/opt/amnezia/openvpn/../../../etc/cron.d/x": b"* * * * * root sh"},
        {"/opt/amnezia/openvpnx/file": b""},
    ],
)
def test_files_outside_the_state_folders_are_refused(files: dict[str, bytes]) -> None:
    with pytest.raises(ArchiveFormatError):
        ContainerArchive(OPENVPN_STATE, tar_gz(files))


def test_links_are_refused() -> None:
    with pytest.raises(ArchiveFormatError):
        ContainerArchive(OPENVPN_STATE, tar_gz({}, symlink="/opt/amnezia/openvpn/link"))


def test_garbage_is_refused() -> None:
    with pytest.raises(ArchiveFormatError):
        ContainerArchive(XRAY_STATE, b"not a tar")
    with pytest.raises(ArchiveFormatError):
        ContainerArchive.decode(XRAY_STATE, "%%%")


def test_repr_hides_the_contents() -> None:
    archive = ContainerArchive(XRAY_STATE, tar_gz({"/opt/amnezia/xray/key": b"secret"}))

    assert "secret" not in repr(archive)
