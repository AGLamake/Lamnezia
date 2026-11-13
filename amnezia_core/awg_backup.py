"""Files of an AmneziaWG 2.0 container rebuilt from saved keys, to bring a server back.

The layout follows upstream configure_container.sh, so the Amnezia app reads the restored
server exactly as one it set up itself.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Interface

from amnezia_core.awg import (
    AWG_CLIENTS_TABLE_PATH,
    AWG_CONFIG_PATH,
    AWG_INTERFACE_PARAMS,
    AWG_PSK_PATH,
    AwgServerConfig,
)
from amnezia_core.containers import AWG2
from amnezia_core.keys import KeyPair
from amnezia_core.wg_config import Section, WgConfig

AWG_PRIVATE_KEY_PATH = AWG2.private_key_path
AWG_PUBLIC_KEY_PATH = AWG2.public_key_path

# Upstream writes I1-I5 as comments: the server does not use them, the app reads them back.
_COMMENTED_PARAMS = frozenset({"I1", "I2", "I3", "I4", "I5"})


def interface_config(
    private_key: str, address: IPv4Interface, port: int, params: Mapping[str, str]
) -> AwgServerConfig:
    """awg0.conf without peers, line for line as configure_container.sh writes it.

    Empty parameters are left out, as the script's `sed` rule drops them.
    """
    lines = [
        "[Interface]",
        f"PrivateKey = {private_key}",
        f"Address = {address}",
        f"ListenPort = {port}",
    ]
    for name in AWG_INTERFACE_PARAMS:
        value = params.get(name, "")
        if value:
            prefix = "# " if name in _COMMENTED_PARAMS else ""
            lines.append(f"{prefix}{name} = {value}")
    return AwgServerConfig(WgConfig([], [Section(lines)], trailing_newline=True))


@dataclass(frozen=True)
class AwgServerFiles:
    """What makes an amnezia-awg2 container the same server: its keys, config and clients."""

    config: str
    clients_table: str
    preshared_key: str

    def by_path(self) -> dict[str, bytes]:
        """Container files in write order: awg0.conf comes last and marks the set complete."""
        private_key = AwgServerConfig.parse(self.config).private_key or ""
        public_key = KeyPair.from_private_key(private_key).public_key
        return {
            AWG_PRIVATE_KEY_PATH: f"{private_key}\n".encode(),
            AWG_PUBLIC_KEY_PATH: f"{public_key}\n".encode(),
            AWG_PSK_PATH: f"{self.preshared_key}\n".encode(),
            AWG_CLIENTS_TABLE_PATH: self.clients_table.encode(),
            AWG_CONFIG_PATH: self.config.encode(),
        }
