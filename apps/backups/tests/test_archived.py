"""Backups of every protocol but AmneziaWG 2.0: a copy of the container folders, put back as is.

Each case installs the protocol on one fake server, copies it into the panel and restores it on
a clean fake server, where the files must be the same and no new keys or CA be made.
"""

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization

from amnezia_core import ipsec, openvpn
from amnezia_core.awg import AwgServerConfig
from amnezia_core.awg_install import AwgInstallSettings
from amnezia_core.container_archive import ContainerArchive
from amnezia_core.containers import AWG_LEGACY, WIREGUARD
from amnezia_core.ipsec import IPSEC_CA_CERT_PATH, IPSEC_CONTAINER
from amnezia_core.openvpn import OPENVPN_CA_CERT_PATH, OPENVPN_CONTAINER
from amnezia_core.tests.pki import FakeCa
from amnezia_core.xray import XRAY_CONFIG_PATH, XRAY_CONTAINER, XRAY_DIR
from apps.backups.errors import RestoreError, RestoreErrorCode
from apps.backups.models import ProtocolBackup
from apps.backups.services.archive_restore import ArchiveRestorer
from apps.backups.services.archived import ArchivedProtocol, archived_protocols
from apps.backups.tests.fake_containers import (
    TarAwgServer,
    TarIpsecHost,
    TarOpenVpnHost,
    TarXrayHost,
)
from apps.keys.models import AccessKey, KeyStatus
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.tests.factories import OperationFactory
from apps.protocols.models import InstanceOrigin, ProtocolInstance
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db

Fake = TarAwgServer | TarOpenVpnHost | TarXrayHost | TarIpsecHost


@dataclass(frozen=True)
class Case:
    """One protocol: its fake server, its files and how the panel knows the server."""

    container: str
    port: int
    subnet: str
    params: dict[str, Any]
    fake: Callable[[], Fake]
    files: Callable[[Fake], dict[str, bytes]]
    # Gives the installed server a real identity and stores it on the instance.
    adopt: Callable[[Fake, ProtocolInstance], None]


def container_files(fake: Fake) -> dict[str, bytes]:
    if isinstance(fake, TarAwgServer):
        return fake.container_files
    if isinstance(fake, TarXrayHost):
        return fake.volume
    return fake.files


def adopt_wg(fake: Fake, instance: ProtocolInstance) -> None:
    assert isinstance(fake, TarAwgServer)
    config = AwgServerConfig.parse(fake.container_files[fake.kind.config_path].decode())
    instance.private_key = config.private_key or ""


def adopt_xray(fake: Fake, instance: ProtocolInstance) -> None:
    assert isinstance(fake, TarXrayHost)
    instance.private_key = json.loads(fake.volume[XRAY_CONFIG_PATH])["inbounds"][0][
        "streamSettings"
    ]["realitySettings"]["privateKey"]


def adopt_openvpn(fake: Fake, instance: ProtocolInstance) -> None:
    pem = FakeCa().pem
    container_files(fake)[OPENVPN_CA_CERT_PATH] = pem.encode()
    instance.public_key = openvpn.ca_fingerprint(pem)


def adopt_ipsec(fake: Fake, instance: ProtocolInstance) -> None:
    der = FakeCa().certificate.public_bytes(serialization.Encoding.DER)
    container_files(fake)[IPSEC_CA_CERT_PATH] = base64.b64encode(der) + b"\n"
    instance.public_key = ipsec.ca_fingerprint(base64.b64encode(der).decode())


CASES = {
    "awg-legacy": Case(
        AWG_LEGACY.name,
        40002,
        "10.8.1.0/24",
        dict(AwgInstallSettings.for_container(AWG_LEGACY, 40002).params),
        lambda: TarAwgServer(kind=AWG_LEGACY, has_image=False),
        container_files,
        adopt_wg,
    ),
    "wireguard": Case(
        WIREGUARD.name,
        51820,
        "10.8.1.0/24",
        {},
        lambda: TarAwgServer(kind=WIREGUARD, has_image=False),
        container_files,
        adopt_wg,
    ),
    "xray": Case(XRAY_CONTAINER, 443, "", {"site": "www.example.com"}, TarXrayHost,
                 container_files, adopt_xray),
    "openvpn": Case(OPENVPN_CONTAINER, 1194, "10.8.0.0/24", {}, TarOpenVpnHost,
                    container_files, adopt_openvpn),
    "ipsec": Case(IPSEC_CONTAINER, 500, "", {}, TarIpsecHost, container_files, adopt_ipsec),
}  # fmt: skip


def context() -> OperationContext:
    return OperationContext(OperationFactory())


def installed(
    case: Case, fake: Fake | None = None
) -> tuple[ProtocolInstance, ArchivedProtocol, Fake]:
    """The protocol installed on a fake server by its own installer, known to the panel."""
    instance = ProtocolInstance(
        server=ServerFactory(),
        container=case.container,
        port=case.port,
        subnet=case.subnet,
        params=case.params,
        origin=InstanceOrigin.INSTALLED,
    )
    protocol = archived_protocols.find(case.container)
    assert protocol is not None
    fake = fake or case.fake()
    protocol.installer(fake.host(), context(), instance).run()
    case.adopt(fake, instance)
    instance.save()
    return instance, protocol, fake


def restore(instance: ProtocolInstance, protocol: ArchivedProtocol, fake: Fake) -> Operation:
    operation = OperationFactory()
    ArchiveRestorer(instance, fake.host(), OperationContext(operation), protocol).run()
    return operation


def log_of(operation: Operation) -> str:
    return "\n".join(operation.steps.values_list("log", flat=True))


@pytest.mark.parametrize("name", CASES)
def test_copy_holds_the_state_folders_of_the_container(name: str) -> None:
    case = CASES[name]
    instance, protocol, fake = installed(case)

    backup = protocol.save(instance, fake.host())

    assert backup is not None
    archive = ContainerArchive.decode(protocol.state, backup.archive)
    assert archive.identity() == protocol.identity(instance)
    saved = {path: content for path, content in case.files(fake).items()}
    for path, content in archive.files().items():
        assert saved[path] == content
    assert protocol.state.identity_path in archive.files()


@pytest.mark.parametrize("name", CASES)
def test_restore_puts_the_same_files_on_a_clean_server(name: str) -> None:
    case = CASES[name]
    instance, protocol, old = installed(case)
    protocol.save(instance, old.host())
    copied = ContainerArchive.decode(protocol.state, instance.backup.archive).files()
    instance.container_removed_at = instance.server.created_at
    instance.save()
    new = case.fake()

    operation = restore(instance, protocol, new)

    restored = case.files(new)
    for path, content in copied.items():
        assert restored[path] == content, path
    instance.refresh_from_db()
    assert not instance.removed
    assert "Files put back" in log_of(operation)


def test_restored_openvpn_keeps_its_ca() -> None:
    instance, protocol, old = installed(CASES["openvpn"])
    protocol.save(instance, old.host())
    new = TarOpenVpnHost()

    restore(instance, protocol, new)

    assert new.pki_runs == 0
    assert new.openvpn_up


def test_restored_ipsec_keeps_its_ca() -> None:
    instance, protocol, old = installed(CASES["ipsec"])
    protocol.save(instance, old.host())
    new = TarIpsecHost()

    restore(instance, protocol, new)

    assert new.pki_runs == 0
    assert new.has_ca and new.pluto_up


@pytest.mark.parametrize("name", CASES)
def test_container_with_other_keys_is_not_copied_over(name: str) -> None:
    case = CASES[name]
    instance, protocol, fake = installed(case)
    protocol.save(instance, fake.host())
    taken_at = instance.backup.taken_at
    # The XRay fake always makes the same keys: the other server is given its own one.
    rival = TarXrayHost(volume={f"{XRAY_DIR}/xray_private.key": b"other-key\n"})
    other, _protocol, other_fake = installed(case, rival if name == "xray" else None)

    assert protocol.save(instance, other_fake.host()) is None
    assert ProtocolBackup.objects.get(instance=instance).taken_at == taken_at

    with pytest.raises(RestoreError) as error:
        restore(instance, protocol, other_fake)
    assert error.value.code == RestoreErrorCode.OTHER_CONTAINER
    assert other.private_key or other.public_key


def test_restore_without_a_copy_is_refused() -> None:
    instance, protocol, _fake = installed(CASES["openvpn"])

    with pytest.raises(RestoreError) as error:
        restore(instance, protocol, TarOpenVpnHost())

    assert error.value.code == RestoreErrorCode.NO_BACKUP


def test_copy_of_other_server_keys_is_refused() -> None:
    instance, protocol, fake = installed(CASES["xray"])
    protocol.save(instance, fake.host())
    ProtocolInstance.objects.filter(pk=instance.pk).update(private_key="another-key")
    instance.refresh_from_db()

    with pytest.raises(RestoreError) as error:
        restore(instance, protocol, TarXrayHost())

    assert error.value.code == RestoreErrorCode.BACKUP_OF_OTHER_SERVER


def test_keys_issued_after_the_copy_are_named_in_the_log() -> None:
    instance, protocol, fake = installed(CASES["wireguard"])
    protocol.save(instance, fake.host())
    key = AccessKey.objects.create(
        source=instance.source, name="Phone", public_key="peer", status=KeyStatus.ACTIVE
    )
    # The clock may not tick between the copy and the key: the key is set a second later.
    AccessKey.objects.filter(pk=key.pk).update(
        created_at=instance.backup.taken_at + timedelta(seconds=1)
    )

    operation = restore(instance, protocol, TarAwgServer(kind=WIREGUARD, has_image=False))

    assert "Keys issued after the copy: 1" in log_of(operation)
