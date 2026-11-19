"""Speed limits of keys and of the whole server as a tc script for a WireGuard-family interface.

Download (server to client) is shaped with HTB on the egress of the interface. Upload is shaped
the same way on an IFB device the ingress of the interface is redirected to; without the ifb
module on the host it is policed on the ingress instead, which drops what goes over the limit.
Rules match the address of the peer.

A personal limit alone gives the peer its own class right under the root, and the traffic of
other peers passes unshaped. A total limit of the server puts every peer the panel knows under
one parent class with that rate: the children have equal rates and borrow up to the total (or
the personal limit), so whoever downloads now gets an equal share. Peers the panel does not know
share one more such class. HTB charges a glued packet in full and only waits longer after it, so
it needs no change for them.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import Any, Self

from amnezia_core.containers import WgContainer

SPEED_SCRIPT_NAME = "lamnezia_speed.sh"
SPEED_SCRIPT_MODE = 0o700

_BYTES_PER_MBIT = 1_000_000 // 8
_KBIT_PER_MBIT = 1000
# amneziawg-go glues TCP segments it receives into packets of up to 64 KB before the interface
# sees them. A policer drops every packet larger than its burst or its mtu, and the mtu tc picks
# by default is about 2 KB, so both are set to let such packets through.
_MAX_PACKET = 65_536
# Upload policing lets this much of a second through at once: TCP slows down when a smaller
# burst drops packets.
_BURST_SHARE = 10
_MIN_BURST = 2 * _MAX_PACKET
# The class of the total limit, and the class of the peers the panel does not know under it.
_TOTAL_CLASS = "1:1"
_OTHERS_MINOR = "ffff"
# Children of the total have equal rates and the same quantum, so they share it equally; a
# fixed quantum also keeps tc quiet about small rates.
_QUANTUM = 1514
_MIN_SHARE_KBIT = 8


@dataclass(frozen=True, order=True)
class SpeedRule:
    """The peer with this address; `mbit` is its own limit each way, None for none."""

    address: IPv4Address
    mbit: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {"address": str(self.address), "mbit": self.mbit}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Self:
        mbit = data.get("mbit")
        return cls(IPv4Address(data["address"]), None if mbit is None else int(mbit))

    @property
    def burst(self) -> int:
        return max((self.mbit or 0) * _BYTES_PER_MBIT // _BURST_SHARE, _MIN_BURST)


@dataclass(frozen=True)
class TotalSpeed:
    """The limit of the whole protocol on the server in megabits per second, each way apart."""

    download: int | None = None
    upload: int | None = None

    def __bool__(self) -> bool:
        return bool(self.download or self.upload)

    def to_json(self) -> dict[str, Any]:
        return {"download": self.download, "upload": self.upload}

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> Self:
        data = data or {}
        return cls(data.get("download") or None, data.get("upload") or None)


NO_TOTAL = TotalSpeed()


def speed_script_path(kind: WgContainer) -> str:
    return f"{kind.directory}/{SPEED_SCRIPT_NAME}"


def ifb_name(interface: str) -> str:
    return f"ifb_{interface}"[:15]


def needs_shaping(rules: Iterable[SpeedRule], total: TotalSpeed) -> bool:
    """Whether anything is limited: without it the script only removes the old rules."""
    return bool(total) or any(rule.mbit for rule in rules)


def speed_script(
    interface: str,
    rules: Iterable[SpeedRule],
    total: TotalSpeed = NO_TOTAL,
    *,
    ifb: bool = False,
) -> str:
    """A script that replaces every tc rule of the interface with these limits; safe to run again.

    `ifb` says the host has the ifb module, so upload is shaped rather than policed.
    """
    rules = sorted(rules, key=lambda rule: rule.address)
    lines = [
        "#!/bin/sh",
        "# Speed limits of Lamnezia keys. The panel rewrites this file; edits are lost.",
        "set -e",
        f"dev={interface}",
        f"ifb={ifb_name(interface)}",
        'tc qdisc del dev "$dev" root 2>/dev/null || true',
        'tc qdisc del dev "$dev" ingress 2>/dev/null || true',
        'ip link del "$ifb" 2>/dev/null || true',
    ]
    lines += _HtbTree('"$dev"', "dst", rules, total.download).lines()
    if ifb:
        upload = _HtbTree('"$ifb"', "src", rules, total.upload)
        if upload.limited:
            lines += [
                'ip link add "$ifb" type ifb',
                'ip link set "$ifb" up',
                'tc qdisc add dev "$dev" handle ffff: ingress',
                'tc filter add dev "$dev" parent ffff: protocol all prio 1 u32'
                ' match u32 0 0 action mirred egress redirect dev "$ifb"',
                *upload.lines(),
            ]
    else:
        lines += _police_lines(rules)
    return "\n".join(lines) + "\n"


class _HtbTree:
    """HTB classes on one device for one direction; `match` is the address field of the peer."""

    def __init__(self, device: str, match: str, rules: list[SpeedRule], total: int | None) -> None:
        self._device = device
        self._match = match
        self._total = total
        # Without a total only peers with their own limit need a class.
        self._rules = rules if total else [rule for rule in rules if rule.mbit]

    @property
    def limited(self) -> bool:
        return bool(self._rules or self._total)

    def lines(self) -> list[str]:
        if not self.limited:
            return []
        if self._total is None:
            return self._personal_lines()
        return self._total_lines(self._total)

    def _personal_lines(self) -> list[str]:
        lines = [f"tc qdisc add dev {self._device} root handle 1: htb"]
        for number, rule in enumerate(self._rules, start=1):
            class_id = f"1:{number:x}"
            lines += [
                f"tc class add dev {self._device} parent 1: classid {class_id}"
                f" htb rate {rule.mbit}mbit ceil {rule.mbit}mbit",
                *self._filter(class_id, rule),
            ]
        return lines

    def _total_lines(self, total: int) -> list[str]:
        share = max(total * _KBIT_PER_MBIT // (len(self._rules) + 1), _MIN_SHARE_KBIT)
        lines = [
            f"tc qdisc add dev {self._device} root handle 1: htb default {_OTHERS_MINOR}",
            f"tc class add dev {self._device} parent 1: classid {_TOTAL_CLASS}"
            f" htb rate {total}mbit ceil {total}mbit",
        ]
        for number, rule in enumerate(self._rules, start=2):
            class_id = f"1:{number:x}"
            ceil = min(rule.mbit, total) if rule.mbit else total
            lines += self._share(class_id, share, ceil) + self._filter(class_id, rule)
        return lines + self._share(f"1:{_OTHERS_MINOR}", share, total)

    def _share(self, class_id: str, share: int, ceil: int) -> list[str]:
        rate = min(share, ceil * _KBIT_PER_MBIT)
        return [
            f"tc class add dev {self._device} parent {_TOTAL_CLASS} classid {class_id}"
            f" htb rate {rate}kbit ceil {ceil}mbit quantum {_QUANTUM}"
        ]

    def _filter(self, class_id: str, rule: SpeedRule) -> list[str]:
        return [
            f"tc filter add dev {self._device} parent 1: protocol ip prio 1 u32"
            f" match ip {self._match} {rule.address}/32 flowid {class_id}"
        ]


def _police_lines(rules: list[SpeedRule]) -> list[str]:
    """Upload limits without IFB: packets of a peer over its limit are dropped."""
    limited = [rule for rule in rules if rule.mbit]
    if not limited:
        return []
    lines = ['tc qdisc add dev "$dev" handle ffff: ingress']
    for rule in limited:
        lines.append(
            f'tc filter add dev "$dev" parent ffff: protocol ip prio 1 u32'
            f" match ip src {rule.address}/32 police rate {rule.mbit}mbit burst {rule.burst}"
            f" mtu {_MAX_PACKET} drop flowid :1"
        )
    return lines
