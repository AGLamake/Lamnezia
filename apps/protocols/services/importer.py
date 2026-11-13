"""Import of a server set up in the Amnezia app: containers, supported protocols, clients.

The import only reads: it neither writes files nor takes the server lock, so the Amnezia app
sees the server exactly as before.
"""

from dataclasses import dataclass
from typing import Any, Protocol

from django.db import transaction
from django.utils.translation import gettext as _

from amnezia_core.awg import AwgServerConfig
from amnezia_core.awg_export import AwgServerSettings
from amnezia_core.clients_table import ClientsTable
from amnezia_core.containers import WG_CONTAINERS, WgContainer
from amnezia_core.docker_ps import DOCKER_PS_FORMAT, RunningContainer, parse_docker_ps
from amnezia_core.errors import ConfigFormatError
from amnezia_core.ipsec import (
    CLIENT_SUBNET,
    IKEV2_CONFIG_PATH,
    IPSEC_CA_CERT_PATH,
    IPSEC_CLIENTS_TABLE_PATH,
    IPSEC_CONTAINER,
    IPSEC_PORTS,
    server_identity,
)
from amnezia_core.ipsec import ca_fingerprint as ipsec_ca_fingerprint
from amnezia_core.keys import KeyPair
from amnezia_core.openvpn import (
    OPENVPN_CA_CERT_PATH,
    OPENVPN_CLIENTS_TABLE_PATH,
    OPENVPN_CONFIG_PATH,
    OPENVPN_CONTAINER,
    OPENVPN_INDEX_PATH,
    CertificateIndex,
    OpenVpnSettings,
    ca_fingerprint,
    is_valid_client_id,
)
from amnezia_core.xray import (
    XRAY_CLIENTS_TABLE_PATH,
    XRAY_CONFIG_PATH,
    XRAY_CONTAINER,
    XRAY_OWN_CLIENT_PATH,
    XRAY_PUBLIC_KEY_PATH,
    XrayServerConfig,
)
from apps.accounts.models import User
from apps.keys.errors import explained_format_errors
from apps.keys.services.adoption import adopt_certificate_client, adopt_peer, adopt_xray_client
from apps.keys.services.ipsec_clients import IpsecDatabase
from apps.keys.services.xray_profiles import follow_short_id
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.protocols.models import (
    CONTAINER_TITLES,
    SERVICE_TITLES,
    InstanceOrigin,
    ProtocolInstance,
)
from apps.protocols.services.service_import import ServiceFiles
from apps.servers.models import Server
from apps.ssh.errors import RemoteFileNotFoundError
from apps.ssh.services import DockerContainer, RemoteHost

IMPORT_KIND = "protocols.import"
# Params the panel sets and the server does not keep, by container: an import leaves them as
# they are. The app keeps them on the device, as the extra client config of OpenVPN.
PANEL_PARAMS = {OPENVPN_CONTAINER: ("additional_client_config", "block_outside_dns")}


class ContainerFiles(Protocol):
    """What the import read from one container, saved by `ServerImporter`."""

    @property
    def container(self) -> str: ...

    def snapshot(self) -> dict[str, Any]:
        """ProtocolInstance fields as the files describe them."""
        ...

    def adopt(self, source: str) -> tuple[int, int]:
        """Take the clients the panel does not know yet as keys: (new, already known)."""
        ...


@dataclass(frozen=True)
class AwgFiles:
    """What the import reads from one WireGuard-family container."""

    kind: WgContainer
    config: AwgServerConfig
    clients: ClientsTable
    preshared_key: str
    port: int | None

    @property
    def container(self) -> str:
        return self.kind.name

    def snapshot(self) -> dict[str, Any]:
        with explained_format_errors():
            settings = AwgServerSettings.from_config(self.config, self.port)
        private_key = self.config.private_key or ""
        return {
            "port": settings.port,
            "subnet": str(settings.subnet.network),
            "params": dict(settings.params),
            "private_key": private_key,
            "public_key": KeyPair.from_private_key(private_key).public_key,
            "preshared_key": self.preshared_key,
        }

    def adopt(self, source: str) -> tuple[int, int]:
        new = 0
        for number, peer in enumerate(self.config.peers, start=1):
            fallback = _("Imported client %(number)d") % {"number": number}
            _key, created = adopt_peer(source, peer, self.clients, fallback)
            new += created
        return new, len(self.config.peers) - new


@dataclass(frozen=True, repr=False)
class XrayFiles:
    """What the import reads from the XRay container; client UUIDs are secrets, `repr` hides them.

    The client the installer created (xray_uuid.key) is taken too, so the panel can show it; it
    is the owner's own access, which the panel never revokes or disables.
    """

    config: XrayServerConfig
    clients: ClientsTable
    own_client_id: str
    public_key: str

    @property
    def container(self) -> str:
        return XRAY_CONTAINER

    def snapshot(self) -> dict[str, Any]:
        with explained_format_errors():
            settings = self.config.settings()
        return {
            "port": settings.port,
            "subnet": "",
            "params": {
                "site": settings.site,
                "sni": settings.sni,
                "flow": settings.flow,
                "fingerprint": settings.fingerprint,
                "short_id": self.config.short_id,
            },
            "private_key": self.config.private_key,
            "public_key": self.public_key,
            "preshared_key": "",
        }

    def adopt(self, source: str) -> tuple[int, int]:
        """Also puts the shortId read now into the profiles of the keys already issued."""
        new = known = 0
        for number, client_id in enumerate(self.config.client_ids, start=1):
            if client_id == self.own_client_id:
                fallback = _("Installer's client")
            else:
                fallback = _("Imported client %(number)d") % {"number": number}
            _key, created = adopt_xray_client(source, client_id, self.clients, fallback)
            new += created
            known += not created
        follow_short_id(source, self.config.short_id)
        return new, known


@dataclass(frozen=True)
class OpenVpnFiles:
    """What the import reads from the OpenVPN container: settings and the issued certificates.

    A client is a certificate in force in pki/index.txt; its private key stays on the device.
    The panel keeps no secret of the CA, only the fingerprint of its certificate.
    """

    settings: OpenVpnSettings
    index: CertificateIndex
    clients: ClientsTable
    ca_fingerprint: str

    @property
    def container(self) -> str:
        return OPENVPN_CONTAINER

    def snapshot(self) -> dict[str, Any]:
        settings = self.settings
        return {
            "port": settings.port,
            "subnet": str(settings.subnet),
            "params": {
                "transport": settings.transport,
                "cipher": settings.cipher,
                "hash": settings.hash,
                "ncp_disable": str(settings.ncp_disable).lower(),
                "tls_auth": str(settings.tls_auth).lower(),
                "additional_server_config": settings.additional_server_config,
            },
            "private_key": "",
            "public_key": self.ca_fingerprint,
            "preshared_key": "",
        }

    def client_ids(self) -> list[str]:
        """Certificates the panel can manage; a name unsafe in easy-rsa commands is left out."""
        return [name for name in self.index.valid_clients() if is_valid_client_id(name)]

    def adopt(self, source: str) -> tuple[int, int]:
        new = known = 0
        for number, client_id in enumerate(self.client_ids(), start=1):
            fallback = _("Imported client %(number)d") % {"number": number}
            _key, created = adopt_certificate_client(source, client_id, self.clients, fallback)
            new += created
            known += not created
        return new, known


@dataclass(frozen=True)
class IpsecFiles:
    """What the import reads from the IPsec container: the server identity and the clients.

    A client is a certificate of the NSS database the CRL does not name. Its key stays in the
    container; the panel keeps no secret of the CA, only the fingerprint of its certificate.
    """

    server_ip: str
    client_ids: list[str]
    clients: ClientsTable
    ca_fingerprint: str

    @property
    def container(self) -> str:
        return IPSEC_CONTAINER

    def snapshot(self) -> dict[str, Any]:
        return {
            "port": IPSEC_PORTS[0],
            "subnet": CLIENT_SUBNET,
            "params": {"server_ip": self.server_ip},
            "private_key": "",
            "public_key": self.ca_fingerprint,
            "preshared_key": "",
        }

    def adopt(self, source: str) -> tuple[int, int]:
        new = known = 0
        for number, client_id in enumerate(self.client_ids, start=1):
            fallback = _("Imported client %(number)d") % {"number": number}
            _key, created = adopt_certificate_client(source, client_id, self.clients, fallback)
            new += created
            known += not created
        return new, known


@dataclass(frozen=True)
class ImportSummary:
    instance: ProtocolInstance | None
    new_keys: int = 0
    known_keys: int = 0


class ServerImporter:
    """Finds the Amnezia containers of a server and imports the supported ones.

    These are amnezia-awg2, amnezia-awg, amnezia-wireguard, amnezia-xray, amnezia-openvpn and
    amnezia-ipsec, and the services
    amnezia-dns, amnezia-sftp and amnezia-torwebsite.

    Running it again updates the protocol settings and adds only clients the panel does not
    know yet; keys already in the panel are left as they are.
    """

    def __init__(
        self,
        server: Server,
        host: RemoteHost,
        ctx: OperationContext,
        *,
        origin: InstanceOrigin = InstanceOrigin.IMPORTED,
    ) -> None:
        """`origin` of a new instance: the install operation records what it installed."""
        self._server = server
        self._host = host
        self._ctx = ctx
        self._origin = origin

    def run(self) -> list[ImportSummary]:
        with self._ctx.step(_("Find Amnezia containers")):
            running = self._find_containers()
        for container in running:
            if container.name in SERVICE_TITLES:
                self._import_service(container)
        return [
            self._import(container) for container in running if container.name in CONTAINER_TITLES
        ]

    def _import_service(self, running: RunningContainer) -> None:
        """The SFTP account and the onion address go to the panel and never to the step log."""
        title = {"title": SERVICE_TITLES[running.name]}
        with self._ctx.step(_("Read %(title)s") % title):
            service = ServiceFiles.read(self._host, running).save(self._server, self._origin)
            if service.app_log_size is not None:
                self._ctx.log(
                    _(
                        "The proxy keeps 3proxy.log, the hosts users visited, left by the Amnezia "
                        "app. Delete it on the server card."
                    )
                )

    def _import(self, running: RunningContainer) -> ImportSummary:
        title = {"title": CONTAINER_TITLES[running.name]}
        with self._ctx.step(_("Read the %(title)s configuration") % title):
            files = self._read_container(running)
        with self._ctx.step(_("Save %(title)s and its clients") % title):
            summary = self._save(files)
            self._ctx.log(
                _("New clients: %(new)d, already in the panel: %(known)d.")
                % {"new": summary.new_keys, "known": summary.known_keys}
            )
        return summary

    def _find_containers(self) -> list[RunningContainer]:
        running = parse_docker_ps(self._host.docker("ps", "--format", DOCKER_PS_FORMAT))
        for container in running:
            known = container.name in CONTAINER_TITLES or container.name in SERVICE_TITLES
            supported = _("supported") if known else _("not supported")
            self._ctx.log(
                f"{container.name} {container.port or ''}/{container.transport}: {supported}"
            )
        if not running:
            self._ctx.log(_("No Amnezia containers are running on the server."))
        self._server.record_containers(
            [{"name": c.name, "port": c.port, "transport": c.transport} for c in running]
        )
        return running

    def _read_container(self, running: RunningContainer) -> ContainerFiles:
        if running.name == XRAY_CONTAINER:
            return self._read_xray()
        if running.name == OPENVPN_CONTAINER:
            return self._read_openvpn()
        if running.name == IPSEC_CONTAINER:
            return self._read_ipsec()
        return self._read(WG_CONTAINERS[running.name], running.port)

    def _read(self, kind: WgContainer, port: int | None) -> AwgFiles:
        container = self._host.container(kind.name)
        config_text = container.read_file(kind.config_path).decode()
        clients_text = _read_optional(container, kind.clients_table_path)
        with explained_format_errors():
            config = AwgServerConfig.parse(config_text)
            if not config.private_key:
                raise ConfigFormatError("the server config has no PrivateKey")
            clients = ClientsTable.parse(clients_text)
        preshared_key = container.read_file(kind.psk_path).decode().strip()
        self._ctx.log(
            _("Clients in the configuration: %(peers)d, named in clientsTable: %(named)d.")
            % {"peers": len(config.peers), "named": len(clients)}
        )
        return AwgFiles(kind, config, clients, preshared_key, port)

    def _read_xray(self) -> XrayFiles:
        container = self._host.container(XRAY_CONTAINER)
        config_text = container.read_file(XRAY_CONFIG_PATH).decode()
        clients_text = _read_optional(container, XRAY_CLIENTS_TABLE_PATH)
        own_client_id = _read_optional(container, XRAY_OWN_CLIENT_PATH).strip()
        # Clients need the Reality public key; the app keeps it next to server.json.
        public_key = container.read_file(XRAY_PUBLIC_KEY_PATH).decode().strip()
        with explained_format_errors():
            files = XrayFiles(
                XrayServerConfig.parse(config_text),
                ClientsTable.parse(clients_text),
                own_client_id,
                public_key,
            )
            files.config.settings()  # only Reality over TCP, which the app sets up
        self._ctx.log(
            _("Clients in the configuration: %(peers)d, named in clientsTable: %(named)d.")
            % {"peers": len(files.config.client_ids), "named": len(files.clients)}
        )
        return files

    def _read_openvpn(self) -> OpenVpnFiles:
        container = self._host.container(OPENVPN_CONTAINER)
        config_text = container.read_file(OPENVPN_CONFIG_PATH).decode()
        index_text = container.read_file(OPENVPN_INDEX_PATH).decode()
        ca_text = container.read_file(OPENVPN_CA_CERT_PATH).decode()
        clients_text = _read_optional(container, OPENVPN_CLIENTS_TABLE_PATH)
        with explained_format_errors():
            files = OpenVpnFiles(
                OpenVpnSettings.from_server_config(config_text),
                CertificateIndex.parse(index_text),
                ClientsTable.parse(clients_text),
                ca_fingerprint(ca_text),
            )
        skipped = len(files.index.valid_clients()) - len(files.client_ids())
        self._ctx.log(
            _("Client certificates in force: %(peers)d, named in clientsTable: %(named)d.")
            % {"peers": len(files.client_ids()), "named": len(files.clients)}
        )
        if skipped:
            self._ctx.log(
                _("Certificates with names the panel cannot manage are skipped: %(count)d.")
                % {"count": skipped}
            )
        return files

    def _read_ipsec(self) -> IpsecFiles:
        container = self._host.container(IPSEC_CONTAINER)
        config_text = container.read_file(IKEV2_CONFIG_PATH).decode()
        ca_text = container.read_file(IPSEC_CA_CERT_PATH).decode()
        clients_text = _read_optional(container, IPSEC_CLIENTS_TABLE_PATH)
        client_ids = IpsecDatabase(container).clients_in_force()
        with explained_format_errors():
            files = IpsecFiles(
                server_identity(config_text),
                client_ids,
                ClientsTable.parse(clients_text),
                ipsec_ca_fingerprint(ca_text),
            )
        self._ctx.log(
            _("Client certificates in force: %(peers)d, named in clientsTable: %(named)d.")
            % {"peers": len(files.client_ids), "named": len(files.clients)}
        )
        return files

    @transaction.atomic
    def _save(self, files: ContainerFiles) -> ImportSummary:
        instance = self._save_instance(files)
        new, known = files.adopt(instance.source)
        return ImportSummary(instance, new, known)

    def _save_instance(self, files: ContainerFiles) -> ProtocolInstance:
        snapshot = {
            **files.snapshot(),
            # The container is on the server again, e.g. set up anew in the Amnezia app.
            "container_removed_at": None,
        }
        snapshot["params"] = {**self._panel_params(files.container), **snapshot["params"]}
        # An instance the panel installed stays "installed" when it is read again.
        instance, _created = ProtocolInstance.objects.update_or_create(
            server=self._server,
            container=files.container,
            defaults=snapshot,
            create_defaults={**snapshot, "origin": self._origin},
        )
        return instance

    def _panel_params(self, container: str) -> dict[str, Any]:
        """Settings the server does not keep, set on the panel before this import."""
        kept = PANEL_PARAMS.get(container, ())
        params = (
            ProtocolInstance.objects.filter(server=self._server, container=container)
            .values_list("params", flat=True)
            .first()
        ) or {}
        return {name: params[name] for name in kept if name in params}


def _read_optional(container: DockerContainer, path: str) -> str:
    try:
        return container.read_file(path).decode()
    except RemoteFileNotFoundError:
        return ""  # no client was ever added in the app


def start_import(server: Server, user: User | None) -> Operation:
    return launch(IMPORT_KIND, {"server_id": server.pk}, user=user, subject=server.name)
