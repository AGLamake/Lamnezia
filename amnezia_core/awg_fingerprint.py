"""What makes the obfuscation of an AmneziaWG server easy to learn: app defaults, shared values.

DPI learns values that many servers share: the defaults every new server of the Amnezia app
gets, the parameters copied from another server, or a header fixed where AmneziaWG 2.0 takes a
range and picks a new value for each packet.
"""

from collections.abc import Mapping

from amnezia_core.awg_install import DEFAULT_OBFUSCATION

_HEADERS = ("H1", "H2", "H3", "H4")


def default_params(params: Mapping[str, str]) -> list[str]:
    """The obfuscation parameters left at the values of the app."""
    return [
        name for name, value in DEFAULT_OBFUSCATION.items() if params.get(name, "").strip() == value
    ]


def fixed_headers(params: Mapping[str, str]) -> list[str]:
    """H1-H4 set to one number instead of a range."""
    return [name for name in _HEADERS if params.get(name, "").strip().isdigit()]


def obfuscation_signature(params: Mapping[str, str]) -> tuple[str, ...]:
    """The obfuscation values in a fixed order, to find servers that share them."""
    return tuple(params.get(name, "").strip() for name in DEFAULT_OBFUSCATION)


def is_default_signature(signature: tuple[str, ...]) -> bool:
    return signature == tuple(DEFAULT_OBFUSCATION.values())
