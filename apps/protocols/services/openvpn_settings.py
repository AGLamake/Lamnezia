"""Changing the transport, port, subnet, cipher, hash, TLS auth and the extra config of OpenVPN.

The Amnezia app reinstalls the container for such a change, with a new certificate authority:
every client is lost. The panel keeps the authority and the client certificates. The container
has no data volume, so a new one is started and /opt/amnezia/openvpn passes into it from the old
one, without server.conf: configure_container.sh writes it anew. The old container waits aside
until openvpn serves. Clients still import their keys again: the profiles carry the settings.
"""

from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass, replace
from ipaddress import IPv4Network
from typing import Any, ClassVar

from django.utils.translation import gettext as _

from amnezia_core.openvpn import (
    DEFAULT_SUBNET,
    DEFAULT_TRANSPORT,
    OPENVPN_CONFIG_PATH,
    OPENVPN_DIR,
    OpenVpnSettings,
)
from apps.keys.errors import explained_format_errors
from apps.keys.services.profile import OpenVpnKeyProfile
from apps.operations.services.context import OperationContext
from apps.protocols.errors import SetupError, SetupErrorCode
from apps.protocols.models import ProtocolInstance
from apps.protocols.services.container import Transport, upstream_file
from apps.protocols.services.container_swap import ContainerSwap
from apps.protocols.services.openvpn_container import OPENVPN_FOLDER, OpenVpnDocker
from apps.protocols.services.openvpn_installer import OpenVpnInstaller
from apps.ssh.services import RemoteHost

OPENVPN_SETTINGS_KIND = "protocols.change_openvpn_settings"
# The certificate authority the copy must bring; without it configure() would make a new one.
_CA_PATH = f"{OPENVPN_DIR}/ca.crt"
# Addresses openvpn gave the clients in the old subnet.
_POOL_PATH = f"{OPENVPN_DIR}/ipp.txt"
# The subnet prefix of the app: the page asks for the address only.
SUBNET_PREFIX = DEFAULT_SUBNET.prefixlen
_TRUE = "true"


@dataclass(frozen=True)
class OpenVpnPanelSettings:
    """What an administrator can change, as the settings page of the app.

    The extra server config goes to server.conf, the extra client config and the DNS block to
    the client profiles only: the app keeps them on the device, so the panel keeps them in the
    instance params.
    """

    kind: ClassVar[str] = OPENVPN_SETTINGS_KIND

    server: OpenVpnSettings
    logs: bool = False
    block_outside_dns: bool = False

    @classmethod
    def of(cls, instance: ProtocolInstance) -> "OpenVpnPanelSettings":
        """From the instance, as the import stores the settings."""
        params = instance.params
        defaults = OpenVpnSettings()
        server = OpenVpnSettings(
            port=instance.port,
            transport=str(params.get("transport", DEFAULT_TRANSPORT)),
            subnet=IPv4Network(instance.subnet) if instance.subnet else DEFAULT_SUBNET,
            cipher=str(params.get("cipher") or defaults.cipher),
            hash=str(params.get("hash") or defaults.hash),
            ncp_disable=params.get("ncp_disable") == _TRUE,
            tls_auth=params.get("tls_auth", _TRUE) == _TRUE,
            additional_client_config=str(params.get("additional_client_config", "")),
            additional_server_config=str(params.get("additional_server_config", "")),
        )
        return cls(server, instance.container_logs, params.get("block_outside_dns") == _TRUE)

    @classmethod
    def from_params(cls, data: Mapping[str, Any]) -> "OpenVpnPanelSettings":
        server = OpenVpnSettings(
            port=int(data["port"]),
            transport=data["transport"],
            subnet=IPv4Network(f"{data['subnet']}/{SUBNET_PREFIX}"),
            cipher=data["cipher"],
            hash=data["hash"],
            ncp_disable=bool(data["ncp_disable"]),
            tls_auth=bool(data["tls_auth"]),
            additional_client_config=str(data.get("additional_client_config", "")),
            additional_server_config=str(data.get("additional_server_config", "")),
        )
        return cls(
            server, bool(data.get("logs", False)), bool(data.get("block_outside_dns", False))
        )

    def to_params(self) -> dict[str, Any]:
        server = self.server
        return {
            "port": server.port,
            "transport": server.transport,
            "subnet": str(server.subnet.network_address),
            "cipher": server.cipher,
            "hash": server.hash,
            "ncp_disable": server.ncp_disable,
            "tls_auth": server.tls_auth,
            "additional_client_config": server.additional_client_config,
            "additional_server_config": server.additional_server_config,
            "block_outside_dns": self.block_outside_dns,
            "logs": self.logs,
        }

    def store(self, instance: ProtocolInstance) -> None:
        """`subnet` and `params` as the import stores them, with the panel's own params."""
        server = self.server
        instance.port = server.port
        instance.subnet = str(server.subnet)
        instance.params = {
            "transport": server.transport,
            "cipher": server.cipher,
            "hash": server.hash,
            "ncp_disable": str(server.ncp_disable).lower(),
            "tls_auth": str(server.tls_auth).lower(),
            "additional_server_config": server.additional_server_config,
            "additional_client_config": server.additional_client_config,
            "block_outside_dns": str(self.block_outside_dns).lower(),
        }
        instance.container_logs = self.logs
        instance.save(update_fields=["port", "subnet", "params", "container_logs"])

    def changes_server(self, other: "OpenVpnPanelSettings") -> bool:
        """server.conf differs: the container is replaced and every client reconnects.

        As hasEqualServerSettings of the app, the extra client config is not part of it.
        """
        return _server_conf(self.server) != _server_conf(other.server)

    def changes_profiles(self, other: "OpenVpnPanelSettings") -> bool:
        """Clients must import their keys again; the subnet and the extra server config stay
        on the server.
        """
        return (
            _client_side(self.server) != _client_side(other.server)
            or self.block_outside_dns != other.block_outside_dns
        )

    def touches_server(self, other: "OpenVpnPanelSettings") -> bool:
        return self.changes_server(other) or self.logs != other.logs

    def updated_profile(self, text: str) -> str:
        """The profile with the new settings."""
        profile = OpenVpnKeyProfile.from_json(text)
        server = self.server
        settings = replace(
            OpenVpnSettings.from_json(profile.server),
            port=server.port,
            transport=server.transport,
            subnet=server.subnet,
            cipher=server.cipher,
            hash=server.hash,
            ncp_disable=server.ncp_disable,
            tls_auth=server.tls_auth,
            additional_client_config=server.additional_client_config,
            additional_server_config=server.additional_server_config,
        )
        updated = replace(
            profile, server=settings.to_json(), block_outside_dns=self.block_outside_dns
        )
        return updated.to_json()


def _server_conf(server: OpenVpnSettings) -> OpenVpnSettings:
    return replace(server, additional_client_config="")


def _client_side(server: OpenVpnSettings) -> OpenVpnSettings:
    return replace(server, subnet=DEFAULT_SUBNET, additional_server_config="")


class OpenVpnReconfiguration:
    """A new amnezia-openvpn with the authority of the old one (DESIGN.md 5.4)."""

    def __init__(self, host: RemoteHost, ctx: OperationContext, server_host: str) -> None:
        self._host = host
        self._ctx = ctx
        self._server_host = server_host
        self._docker = OpenVpnDocker(host, ctx)

    def apply(self, settings: OpenVpnPanelSettings) -> None:
        server = settings.server
        installer = OpenVpnInstaller.for_settings(
            self._host, self._ctx, server, server_host=self._server_host, logs=settings.logs
        )
        installer.check_server()
        with ExitStack() as stack:
            with self._ctx.step(_("Wait for other operations on the server")):
                stack.enter_context(self._host.lock())
            with self._ctx.step(_("Read the server config")):
                current = self._read_settings()
            moved = (current.port, current.transport) != (server.port, server.transport)
            new_ports = [(server.port, Transport(server.transport))] if moved else []
            swap = ContainerSwap(self._host, self._ctx, self._docker)
            with swap.replace(new_ports):
                if not self._docker.has_image():
                    self._docker.build(upstream_file(f"{OPENVPN_FOLDER}/Dockerfile"))
                self._docker.run_container(installer.run_arguments())
                self._docker.connect_network()
                swap.copy_from_previous(OPENVPN_DIR)
                self._drop_old_config(subnet_changed=current.subnet != server.subnet)
                installer.start(installer.configure())

    def _drop_old_config(self, *, subnet_changed: bool) -> None:
        """The copy must hold the authority; server.conf is written anew from the settings."""
        if not self._docker.has_file(_CA_PATH):
            raise SetupError(SetupErrorCode.OPENVPN_PKI_NOT_COPIED, _CA_PATH)
        stale = [OPENVPN_CONFIG_PATH, *([_POOL_PATH] if subnet_changed else [])]
        self._docker.shell.run(f"rm -f {' '.join(stale)}")
        self._ctx.log(_("The certificate authority and the clients passed to the new container."))

    def _read_settings(self) -> OpenVpnSettings:
        text = self._docker.shell.read_file(OPENVPN_CONFIG_PATH).decode()
        with explained_format_errors():
            return OpenVpnSettings.from_server_config(text)
