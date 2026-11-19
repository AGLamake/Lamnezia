"""Settings of a new AmneziaWG or WireGuard server, generated the way the Amnezia app does it.

Mirrors upstream AwgInstaller::generateAwgParameters, protocolConstants.h (namespaces awg
and wireguard), amnezia::genAwgVars and genWireGuardVars. The Amnezia app reads them back
from the server config.
"""

import random
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from ipaddress import IPv4Network
from typing import Self

from amnezia_core.awg_export import has_awg3_params
from amnezia_core.awg_masking import Masking, is_app_masking
from amnezia_core.awg_params import MESSAGE_SIZES, MIN_PADDING
from amnezia_core.containers import AWG2, WgContainer
from amnezia_core.errors import AmneziaFormatError
from amnezia_core.keys import KeyPair
from amnezia_core.script_vars import (
    PARAM_VARIABLES,
    TOGGLES,
    container_variables,
    is_toggle_enabled,
)

DEFAULT_PORT = 55424
WIREGUARD_DEFAULT_PORT = 51820
DEFAULT_SUBNET = IPv4Network("10.8.1.0/24")

# Jc is random in [4, 6] (QRandomGenerator::bounded(4, 7)); the rest are upstream defaults.
_JUNK_PACKET_COUNT = (4, 6)
_PADDING = str(MIN_PADDING)
# The smallest client subnet that still leaves addresses for clients after the server's one.
_MAX_PREFIX = 30
_MAX_PORT = 65535
_FIXED_PARAMS = {
    "Jmin": "10",
    "Jmax": "50",
    "S1": _PADDING,
    "S2": _PADDING,
    "S3": _PADDING,
    "S4": _PADDING,
    "H1": "1",
    "H2": "2",
    "H3": "3",
    "H4": "4",
    "RekeyAfterTime": "100-120",
    "RekeyTimeout": "3-7",
    "RejectAfterTime": "150-180",
    "KeepaliveTimeout": "5-15",
    "MaxHandshakeAttempts": "15-20",
    "RandomTrailers": "on",
    "DisableCookies": "on",
}
# Timers of AmneziaWG 3.1 that make a client behind a relay reach a new server in 7-9 s instead
# of 18-22 s (measured in P12-01): the outage is the upper bound of KeepaliveTimeout plus
# RekeyTimeout. On lossy mobile networks they cost extra handshakes.
SHORT_TIMERS = {"RekeyTimeout": "2-3", "KeepaliveTimeout": "3-5"}
DEFAULT_TIMERS = {name: _FIXED_PARAMS[name] for name in SHORT_TIMERS}


def without_timers(params: Mapping[str, str]) -> dict[str, str]:
    """`params` without the SHORT_TIMERS parameters.

    Each side runs its own timers and the handshake does not compare them: a profile with the
    old timers keeps connecting after the server changes only them, either way.
    """
    return {name: value for name, value in params.items() if name not in SHORT_TIMERS}


def has_short_timers(params: Mapping[str, str]) -> bool:
    """The parameters carry SHORT_TIMERS."""
    return all(params.get(name, "").strip() == value for name, value in SHORT_TIMERS.items())


def generate_awg_params(
    rng: random.Random | None = None, masking: Masking | None = None
) -> dict[str, str]:
    """Obfuscation parameters for awg0.conf; ContentPaddingAddition stays unset, as upstream.

    I1 follows `masking`, a random template without it, instead of the one packet of the app.
    """
    rng = rng or secrets.SystemRandom()
    return {
        "Jc": str(rng.randint(*_JUNK_PACKET_COUNT)),
        **_FIXED_PARAMS,
        "I1": (masking or Masking.pick(rng)).packet(rng),
        "HeaderProtectionKey": KeyPair.generate().private_key,
    }


def upgraded_awg_params(
    params: Mapping[str, str], masking: Masking | None = None
) -> dict[str, str]:
    """AmneziaWG 2.0 parameters moved to 3.1: what a new server gets for 3.1 is added.

    Values already set stay, except the I1 of the app: it gives way to a template, as an empty
    one does. Header protection needs every S1-S4 of at least 12 bytes (amneziawg-go refuses
    less); a smaller one grows to the nearest size that keeps the four messages of different
    lengths.
    """
    generated = generate_awg_params(masking=masking)
    added = {
        name: value
        for name, value in generated.items()
        if has_awg3_params({name: value}) and not params.get(name, "").strip()
    }
    upgraded = {**params, **added}
    current = upgraded.get("I1", "").strip()
    if not current or is_app_masking(current):
        upgraded["I1"] = generated["I1"]
    for name, message in MESSAGE_SIZES.items():
        others = {
            int(upgraded.get(other) or 0) + size
            for other, size in MESSAGE_SIZES.items()
            if other != name
        }
        padding = max(int(upgraded.get(name) or 0), MIN_PADDING)
        while padding + message in others:
            padding += 1
        upgraded[name] = str(padding)
    return upgraded


@dataclass(frozen=True)
class AwgInstallSettings:
    """What configure_container.sh and start.sh need: port, client subnet, AWG parameters.

    WireGuard has no AWG parameters: `for_container` leaves them empty.
    """

    port: int = DEFAULT_PORT
    subnet: IPv4Network = DEFAULT_SUBNET
    params: Mapping[str, str] = field(default_factory=generate_awg_params, repr=False)
    container: WgContainer = AWG2

    def __post_init__(self) -> None:
        if not 1 <= self.port <= _MAX_PORT:
            raise AmneziaFormatError(f"Port {self.port} is out of range.")
        if self.subnet.prefixlen > _MAX_PREFIX:
            raise AmneziaFormatError(f"Subnet {self.subnet} is too small for clients.")

    @classmethod
    def for_container(
        cls, container: WgContainer, port: int, masking: Masking | None = None
    ) -> Self:
        """Defaults of the Amnezia app for `container` on `port`, I1 by `masking`."""
        params = generate_awg_params(masking=masking) if container.obfuscated else {}
        return cls(port=port, params=params, container=container)

    def script_variables(self) -> dict[str, str]:
        """Values for the `$NAME` placeholders of the upstream server scripts."""
        subnet_ip, port = str(self.subnet.network_address), str(self.port)
        variables = {
            **container_variables(self.container.name),
            "$AWG_SUBNET_IP": subnet_ip,
            "$WIREGUARD_SUBNET_IP": subnet_ip,
            "$WIREGUARD_SUBNET_CIDR": str(self.subnet.prefixlen),
            "$AWG_SERVER_PORT": port,
            "$WIREGUARD_SERVER_PORT": port,
        }
        for variable, name in PARAM_VARIABLES.items():
            value = self.params.get(name, "")
            # genAwgVars passes a switched-off toggle as an empty value, so the line is dropped.
            if name in TOGGLES and not is_toggle_enabled(value):
                value = ""
            variables[variable] = value
        return variables
