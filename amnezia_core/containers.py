"""WireGuard-family containers of the Amnezia app: AmneziaWG 2.0, legacy AmneziaWG and WireGuard."""

from dataclasses import dataclass


@dataclass(frozen=True)
class WgContainer:
    """Where a container keeps its files and which tool manages its interface.

    Values follow upstream protocolConstants.h, server_scripts/<type>/ and
    UsersController::revokeWireGuard (`awg` and `awg0` only for AmneziaWG 2.0).
    """

    name: str
    protocol: str  # key of the protocol object in the vpn:// JSON
    directory: str
    config_name: str
    tool: str  # `awg` or `wg`, with the matching `<tool>-quick`
    obfuscated: bool  # has the AmneziaWG Jc/S/H parameters

    @property
    def interface(self) -> str:
        return self.config_name.removesuffix(".conf")

    @property
    def config_path(self) -> str:
        return f"{self.directory}/{self.config_name}"

    @property
    def clients_table_path(self) -> str:
        return f"{self.directory}/clientsTable"

    @property
    def psk_path(self) -> str:
        return f"{self.directory}/wireguard_psk.key"

    @property
    def private_key_path(self) -> str:
        return f"{self.directory}/wireguard_server_private_key.key"

    @property
    def public_key_path(self) -> str:
        return f"{self.directory}/wireguard_server_public_key.key"


_AWG_DIR = "/opt/amnezia/awg"
AWG2 = WgContainer("amnezia-awg2", "awg", _AWG_DIR, "awg0.conf", "awg", obfuscated=True)
AWG_LEGACY = WgContainer("amnezia-awg", "awg", _AWG_DIR, "wg0.conf", "wg", obfuscated=True)
WIREGUARD = WgContainer(
    "amnezia-wireguard", "wireguard", "/opt/amnezia/wireguard", "wg0.conf", "wg", obfuscated=False
)
# Every WireGuard-family container by name, in the order the Amnezia app lists them.
WG_CONTAINERS = {container.name: container for container in (AWG2, AWG_LEGACY, WIREGUARD)}
