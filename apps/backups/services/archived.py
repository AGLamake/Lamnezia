"""Backups of the protocols kept as a copy of their container folders: one class per protocol.

AmneziaWG 2.0 has its own backup (`snapshot`, `restore`); every other protocol with its own
state is an `ArchivedProtocol` registered in `archived_protocols`. The copy holds the files
the upstream scripts and the app wrote, so a container set up from it is the same server.
"""

import logging
from abc import ABC, abstractmethod
from ipaddress import IPv4Network
from typing import Any, ClassVar

from django.utils import timezone

from amnezia_core.awg_install import AwgInstallSettings
from amnezia_core.container_archive import (
    AWG_LEGACY_STATE,
    IPSEC_STATE,
    OPENVPN_STATE,
    WIREGUARD_STATE,
    XRAY_STATE,
    ContainerArchive,
    ContainerState,
)
from amnezia_core.containers import AWG_LEGACY, WIREGUARD, WgContainer
from apps.backups.errors import RestoreError, RestoreErrorCode
from apps.backups.models import ProtocolBackup
from apps.backups.services.guard import IdentityGuard
from apps.operations.services.context import OperationContext
from apps.protocols.models import ProtocolInstance
from apps.protocols.services.awg_installer import (
    AwgLegacyInstaller,
    WgInstaller,
    WireGuardInstaller,
)
from apps.protocols.services.installer import ContainerGuard, ContainerInstaller
from apps.protocols.services.ipsec_installer import IpsecInstaller
from apps.protocols.services.openvpn_installer import OpenVpnInstaller
from apps.protocols.services.xray_installer import XrayInstaller
from apps.ssh.errors import ErrorCode, SshCommandError
from apps.ssh.services import RemoteHost

logger = logging.getLogger(__name__)


def read_archive(host: RemoteHost, state: ContainerState) -> ContainerArchive:
    """A copy of the state folders of a running container."""
    result = host.container(state.container).sh(state.archive_command(), timeout=state.timeout)
    if not result.ok:
        raise SshCommandError(ErrorCode.COMMAND_FAILED, f"tar exit {result.exit_code}")
    return ContainerArchive(state, result.stdout)


class ArchivedProtocol(ABC):
    """How the container of one protocol is copied, and installed again from the copy.

    A subclass names the `state` of its container and builds its installer from the
    settings the panel keeps for the instance.
    """

    state: ClassVar[ContainerState]

    def identity(self, instance: ProtocolInstance) -> str:
        """What the panel knows the server by: the private key, or the CA fingerprint."""
        return str(instance.private_key)

    @abstractmethod
    def installer(
        self, host: RemoteHost, ctx: OperationContext, instance: ProtocolInstance
    ) -> ContainerInstaller[Any]:
        """The installer that sets the container up again on the server of the instance."""

    def guard(self, ctx: OperationContext, instance: ProtocolInstance) -> ContainerGuard:
        """The check of a container already on the server, before the restore starts it."""
        return IdentityGuard(
            ctx,
            path=self.state.identity_path,
            identity=self.state.identity,
            expected=self.identity(instance),
            refusal=RestoreError(RestoreErrorCode.OTHER_CONTAINER, instance.source),
        )

    def read(self, host: RemoteHost) -> ContainerArchive:
        """A copy of the state folders of the running container."""
        return read_archive(host, self.state)

    def save(self, instance: ProtocolInstance, host: RemoteHost) -> ProtocolBackup | None:
        """Copy the container into the panel.

        A container with another server key or CA is not the one the panel keys belong to:
        it does not replace the copy, None is returned.
        """
        archive = self.read(host)
        if archive.identity() != self.identity(instance):
            logger.warning("%s has another server identity, its backup is kept", instance.source)
            return None
        backup, _created = ProtocolBackup.objects.update_or_create(
            instance=instance,
            defaults={"archive": archive.encode(), "taken_at": timezone.now()},
        )
        return backup


class _WgFamilyBackup(ArchivedProtocol):
    """WireGuard and AmneziaWG 1.x: wg0.conf with the server key and peers, clientsTable."""

    kind: ClassVar[WgContainer]
    installer_class: ClassVar[type[WgInstaller]]

    def installer(
        self, host: RemoteHost, ctx: OperationContext, instance: ProtocolInstance
    ) -> ContainerInstaller[Any]:
        settings = AwgInstallSettings(
            port=instance.port,
            subnet=IPv4Network(instance.subnet),
            params=instance.params,
            container=self.kind,
        )
        return self.installer_class(host, ctx, settings, logs=instance.container_logs)


class _PkiBackup(ArchivedProtocol):
    """A protocol whose clients trust the CA of the container; the panel keeps its fingerprint."""

    def identity(self, instance: ProtocolInstance) -> str:
        return instance.public_key


class AwgLegacyBackup(_WgFamilyBackup):
    state = AWG_LEGACY_STATE
    kind = AWG_LEGACY
    installer_class = AwgLegacyInstaller


class WireGuardBackup(_WgFamilyBackup):
    state = WIREGUARD_STATE
    kind = WIREGUARD
    installer_class = WireGuardInstaller


class XrayBackup(ArchivedProtocol):
    """server.json with the Reality keys and clients, clientsTable and the key files."""

    state = XRAY_STATE

    def installer(
        self, host: RemoteHost, ctx: OperationContext, instance: ProtocolInstance
    ) -> ContainerInstaller[Any]:
        params = {
            "port": instance.port,
            "site": instance.params.get("site"),
            "logs": instance.container_logs,
        }
        return XrayInstaller.from_params(host, ctx, params)


class OpenVpnBackup(_PkiBackup):
    """The easy-rsa PKI (CA, index.txt, CRL, issued certificates), server.conf and ta.key."""

    state = OPENVPN_STATE

    def installer(
        self, host: RemoteHost, ctx: OperationContext, instance: ProtocolInstance
    ) -> ContainerInstaller[Any]:
        return OpenVpnInstaller(
            host,
            ctx,
            port=instance.port,
            server_host=instance.server.host,
            logs=instance.container_logs,
        )


class IpsecBackup(_PkiBackup):
    """The NSS database with the CA and the certificates, ikev2.conf and the client files."""

    state = IPSEC_STATE

    def installer(
        self, host: RemoteHost, ctx: OperationContext, instance: ProtocolInstance
    ) -> ContainerInstaller[Any]:
        return IpsecInstaller(
            host, ctx, server_host=instance.server.host, logs=instance.container_logs
        )


class ArchivedProtocols:
    """The archived protocols by container name."""

    def __init__(self, *protocols: ArchivedProtocol) -> None:
        self._protocols = {protocol.state.container: protocol for protocol in protocols}

    def find(self, container: str) -> ArchivedProtocol | None:
        return self._protocols.get(container)

    def containers(self) -> tuple[str, ...]:
        return tuple(self._protocols)


archived_protocols = ArchivedProtocols(
    AwgLegacyBackup(), WireGuardBackup(), XrayBackup(), OpenVpnBackup(), IpsecBackup()
)
