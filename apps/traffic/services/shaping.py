"""Speed limits of keys as a tc script for the interface of a WireGuard-family container.

Download (server to client) is shaped on the egress of the interface with HTB; upload is policed
on its ingress, which needs no extra kernel module (an IFB device would). Rules match the address
of the peer. Traffic of other peers matches no class and passes unshaped. HTB charges a glued
packet in full and only waits longer after it, so it needs no change for them.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import Any, Self

from amnezia_core.containers import WgContainer

SPEED_SCRIPT_NAME = "lamnezia_speed.sh"
SPEED_SCRIPT_MODE = 0o700

_BYTES_PER_MBIT = 1_000_000 // 8
# amneziawg-go glues TCP segments it receives into packets of up to 64 KB before the interface
# sees them. A policer drops every packet larger than its burst or its mtu, and the mtu tc picks
# by default is about 2 KB, so both are set to let such packets through.
_MAX_PACKET = 65_536
# Upload policing lets this much of a second through at once: TCP slows down when a smaller
# burst drops packets.
_BURST_SHARE = 10
_MIN_BURST = 2 * _MAX_PACKET


@dataclass(frozen=True, order=True)
class SpeedRule:
    """At most `mbit` megabits per second each way for the peer with this address."""

    address: IPv4Address
    mbit: int

    def to_json(self) -> dict[str, Any]:
        return {"address": str(self.address), "mbit": self.mbit}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Self:
        return cls(IPv4Address(data["address"]), int(data["mbit"]))

    @property
    def burst(self) -> int:
        return max(self.mbit * _BYTES_PER_MBIT // _BURST_SHARE, _MIN_BURST)


def speed_script_path(kind: WgContainer) -> str:
    return f"{kind.directory}/{SPEED_SCRIPT_NAME}"


def speed_script(interface: str, rules: Iterable[SpeedRule]) -> str:
    """A script that replaces every tc rule of the interface with `rules`; safe to run again."""
    lines = [
        "#!/bin/sh",
        "# Speed limits of Lamnezia keys. The panel rewrites this file; edits are lost.",
        "set -e",
        f"dev={interface}",
        'tc qdisc del dev "$dev" root 2>/dev/null || true',
        'tc qdisc del dev "$dev" ingress 2>/dev/null || true',
    ]
    rules = sorted(rules)
    if rules:
        lines += [
            'tc qdisc add dev "$dev" root handle 1: htb',
            'tc qdisc add dev "$dev" handle ffff: ingress',
        ]
    for number, rule in enumerate(rules, start=1):
        lines += _rule_lines(f"1:{number:x}", rule)
    return "\n".join(lines) + "\n"


def _rule_lines(class_id: str, rule: SpeedRule) -> list[str]:
    rate = f"{rule.mbit}mbit"
    return [
        f'tc class add dev "$dev" parent 1: classid {class_id} htb rate {rate} ceil {rate}',
        f'tc filter add dev "$dev" parent 1: protocol ip prio 1 u32'
        f" match ip dst {rule.address}/32 flowid {class_id}",
        f'tc filter add dev "$dev" parent ffff: protocol ip prio 1 u32'
        f" match ip src {rule.address}/32 police rate {rate} burst {rule.burst}"
        f" mtu {_MAX_PACKET} drop flowid :1",
    ]
