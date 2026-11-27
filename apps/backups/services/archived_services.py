"""Backups of the services with their own state: one `ArchivedService` class per service.

The Tor site, MTProxy, Telemt and SFTP keep in their containers what those who use them know
them by: the onion keys, the secret of the links, the SSH host keys. The copy holds these files
(amnezia_core.container_archive); a container installed from it is the same service.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from django.utils.translation import gettext as _

from amnezia_core.container_archive import (
    MTPROXY_STATE,
    SFTP_STATE,
    TELEMT_STATE,
    TOR_STATE,
    ArchiveFormatError,
    ArchiveTooLargeError,
    ContainerArchive,
    ContainerState,
    StateFingerprint,
)
from amnezia_core.errors import AmneziaFormatError
from amnezia_core.proxies import (
    MTPROXY_META_PATH,
    MTPROXY_PORT,
    TELEMT_CONFIG_PATH,
    TELEMT_PORT,
    TelemtConfig,
    parse_meta,
)
from amnezia_core.proxy_options import TelegramProxyOptions
from amnezia_core.services import SFTP_COMMAND_FORMAT, SFTP_PORT, SftpAccount
from apps.backups.errors import RestoreError, RestoreErrorCode
from apps.backups.models import ServiceBackup
from apps.backups.services.archived import read_archive
from apps.backups.services.guard import IdentityGuard
from apps.backups.services.service_copies import service_copies
from apps.operations.services.context import OperationContext
from apps.protocols.models import ServiceInstance
from apps.protocols.services.container import ProtocolContainer
from apps.protocols.services.installer import ContainerGuard, ContainerInstaller
from apps.protocols.services.proxy_installers import MtProxyInstaller, TelemtInstaller
from apps.protocols.services.service_installers import SftpInstaller, TorSiteInstaller
from apps.ssh.services import RemoteHost

logger = logging.getLogger(__name__)


class ArchivedService(ABC):
    """How the container of one service is copied, checked and installed again from the copy.

    The panel knows a service by `ServiceInstance.secret`: the onion address or the proxy
    secret. A subclass names its `state` and builds the installer from the service and the copy.
    """

    state: ClassVar[ContainerState]
    # The service makes its keys itself when it starts: the copy replaces them, and the
    # container is restarted to read it.
    makes_own_keys: ClassVar[bool] = False

    @abstractmethod
    def installer(
        self,
        host: RemoteHost,
        ctx: OperationContext,
        service: ServiceInstance,
        copy: ContainerArchive,
    ) -> ContainerInstaller[Any]:
        """The installer that sets the container up again with the settings of the copy."""

    def identity(self, service: ServiceInstance) -> str:
        return str(service.secret)

    def copy_matches(self, service: ServiceInstance, copy: ContainerArchive) -> bool:
        """Whether a saved copy holds the keys of the service the panel knows."""
        return copy.identity() == self.identity(service)

    def guard(self, ctx: OperationContext, service: ServiceInstance) -> ContainerGuard:
        """The check of a container already on the server, before the restore starts it."""
        return IdentityGuard(
            ctx,
            path=self.state.identity_path,
            identity=self.state.identity,
            expected=self.identity(service),
            refusal=RestoreError(RestoreErrorCode.OTHER_CONTAINER, str(service)),
        )

    def save(
        self, service: ServiceInstance, host: RemoteHost, *, changed: bool = False
    ) -> ServiceBackup | None:
        """Copy the container into the panel; one with other keys keeps the last copy (None).

        The files are fingerprinted on the server first. Unchanged since the last copy, they
        are not sent again; larger than the copy may hold, they are not sent at all, and the
        panel shows it. After a change the panel made (`changed`) they are most likely new:
        the server reads them once, for the copy, and the hash of the copy tells whether they
        differ from the saved one. A copy of the same files does not replace the saved one.
        """
        fingerprint = None if changed else self._fingerprint(host)
        if fingerprint is not None:
            if fingerprint.size > self.state.max_size:
                return self._too_large(service, fingerprint.size)
            if service_copies.holds(service, fingerprint.digest):
                return service_copies.checked(service)
        try:
            copy = read_archive(host, self.state)
        except ArchiveTooLargeError as error:
            return self._too_large(service, error.size)
        with copy:
            if not self._runs_service(service, host, copy):
                logger.warning("%s has other keys, its backup is kept", self.state.container)
                return None
            return self._kept(service, copy, fingerprint)

    @staticmethod
    def _kept(
        service: ServiceInstance, copy: ContainerArchive, fingerprint: StateFingerprint | None
    ) -> ServiceBackup:
        """Save the copy, unless it holds the files of the saved one: then that one stays."""
        if fingerprint is not None:  # already compared with the saved copy
            return service_copies.save(service, copy, fingerprint.digest)
        digest = copy.digest()
        if service_copies.holds(service, digest):
            return service_copies.checked(service)
        return service_copies.save(service, copy, digest)

    def _fingerprint(self, host: RemoteHost) -> StateFingerprint | None:
        container = host.container(self.state.container)
        result = container.sh(self.state.fingerprint_command(), timeout=self.state.timeout)
        if not result.ok:
            return None
        try:
            return StateFingerprint.parse(result.output)
        except ArchiveFormatError:
            return None

    def _too_large(self, service: ServiceInstance, size: int) -> ServiceBackup:
        logger.warning(
            "%s is not copied: %d bytes, the limit is %d",
            self.state.container,
            size,
            self.state.max_size,
        )
        return service_copies.too_large(service, size)

    def _runs_service(
        self, service: ServiceInstance, host: RemoteHost, copy: ContainerArchive
    ) -> bool:
        return self.copy_matches(service, copy)


def _copied_text(copy: ContainerArchive, path: str) -> str:
    return (copy.read(path) or b"").decode(errors="replace")


class TorSiteBackup(ArchivedService):
    """The keys of the hidden service; Tor reads them when it starts."""

    state = TOR_STATE
    makes_own_keys = True

    def installer(
        self,
        host: RemoteHost,
        ctx: OperationContext,
        service: ServiceInstance,
        copy: ContainerArchive,
    ) -> ContainerInstaller[Any]:
        return TorSiteInstaller(host, ctx, port=service.port or TorSiteInstaller.default_port)


class MtProxyBackup(ArchivedService):
    """The data volume: the secret and /data/mtproxy-meta with the settings of the install."""

    state = MTPROXY_STATE

    def installer(
        self,
        host: RemoteHost,
        ctx: OperationContext,
        service: ServiceInstance,
        copy: ContainerArchive,
    ) -> ContainerInstaller[Any]:
        meta = parse_meta(_copied_text(copy, MTPROXY_META_PATH))
        options = TelegramProxyOptions.from_mtproxy_meta(meta)
        return MtProxyInstaller(host, ctx, port=service.port or MTPROXY_PORT, options=options)


class TelemtBackup(ArchivedService):
    """The data volume: the secret and /data/config.toml with the settings and the switches."""

    state = TELEMT_STATE

    def installer(
        self,
        host: RemoteHost,
        ctx: OperationContext,
        service: ServiceInstance,
        copy: ContainerArchive,
    ) -> ContainerInstaller[Any]:
        config = TelemtConfig.parse(_copied_text(copy, TELEMT_CONFIG_PATH))
        options = TelegramProxyOptions.from_telemt_config(config)
        port = service.port or config.port or TELEMT_PORT
        installer = TelemtInstaller(host, ctx, port=port, options=options)
        installer.switches = config.switches
        return installer


class SftpBackup(ArchivedService):
    """The SSH host keys; the account is the login and password the panel keeps.

    sshd reads the host keys when it starts. The account is in the `docker run` command, so a
    container is the service when it runs with that account.
    """

    state = SFTP_STATE
    makes_own_keys = True

    def installer(
        self,
        host: RemoteHost,
        ctx: OperationContext,
        service: ServiceInstance,
        copy: ContainerArchive,
    ) -> ContainerInstaller[Any]:
        port = service.port or SFTP_PORT
        return SftpInstaller(host, ctx, port=port, account=self.account(service))

    @staticmethod
    def account(service: ServiceInstance) -> SftpAccount:
        return SftpAccount(service.login, service.secret)

    def copy_matches(self, service: ServiceInstance, copy: ContainerArchive) -> bool:
        copy.identity()  # the host key is there; nothing in the panel names it
        return True

    def guard(self, ctx: OperationContext, service: ServiceInstance) -> ContainerGuard:
        return SftpAccountGuard(ctx, self.account(service), str(service))

    def _runs_service(
        self, service: ServiceInstance, host: RemoteHost, copy: ContainerArchive
    ) -> bool:
        command = host.docker("inspect", "--format", SFTP_COMMAND_FORMAT, self.state.container)
        return _account_of(command) == self.account(service)


def _account_of(command: str) -> SftpAccount | None:
    try:
        return SftpAccount.from_command(command)
    except AmneziaFormatError:
        return None


class SftpAccountGuard:
    """Refuses an SFTP container that runs with another account than the panel keeps."""

    def __init__(self, ctx: OperationContext, account: SftpAccount, source: str) -> None:
        self._ctx = ctx
        self._account = account
        self._source = source

    def check(self, docker: ProtocolContainer) -> None:
        if _account_of(docker.inspect(SFTP_COMMAND_FORMAT)) != self._account:
            raise RestoreError(RestoreErrorCode.OTHER_CONTAINER, self._source)
        self._ctx.log(_("The container has the account the panel keeps."))


class ArchivedServices:
    """The archived services by container name."""

    def __init__(self, *services: ArchivedService) -> None:
        self._services = {service.state.container: service for service in services}

    def find(self, container: str) -> ArchivedService | None:
        return self._services.get(container)

    def containers(self) -> tuple[str, ...]:
        return tuple(self._services)


archived_services = ArchivedServices(TorSiteBackup(), MtProxyBackup(), TelemtBackup(), SftpBackup())
