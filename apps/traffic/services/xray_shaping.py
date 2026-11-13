"""Speed limits of XRay keys as socket marks and an nftables file for the XRay container.

Xray has no per-client speed limit, and the address of a client is that of its connection, so a
rule by address (as for WireGuard peers) does not fit. Instead server.json routes each limited
client to an outbound with its own socket mark (`XrayServerConfig.set_speed_marks`), and nftables
in the container polices the traffic of that mark: outgoing packets by the mark, replies by the
connection mark copied from it. Both TCP and UDP connections of xray are tracked, so both are
limited each way (docs/DESIGN.md, 3.9).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Self

from amnezia_core.xray import XRAY_DIR

SPEED_NFT_PATH = f"{XRAY_DIR}/lamnezia_speed.nft"
SPEED_NFT_MODE = 0o600

_TABLE = "inet lamnezia"
_FIRST_MARK = 1001
_KBYTES_PER_MBIT = 125
# Like the WireGuard upload policer: a tenth of a second at once, and at least the burst that
# kept TCP at the limit in the measurements.
_BURST_SHARE = 10
_MIN_BURST_KBYTES = 128


@dataclass(frozen=True, order=True)
class XraySpeedRule:
    """At most `mbit` megabits per second each way for the client with this `email`.

    The email is the fingerprint of the client UUID, the `public_key` of its key.
    """

    email: str
    mbit: int

    def to_json(self) -> dict[str, Any]:
        return {"email": self.email, "mbit": self.mbit}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Self:
        return cls(str(data["email"]), int(data["mbit"]))

    @property
    def rate(self) -> str:
        return f"{self.mbit * _KBYTES_PER_MBIT} kbytes/second"

    @property
    def burst(self) -> str:
        kbytes = max(self.mbit * _KBYTES_PER_MBIT // _BURST_SHARE, _MIN_BURST_KBYTES)
        return f"{kbytes} kbytes"


def speed_marks(rules: Iterable[XraySpeedRule]) -> dict[XraySpeedRule, int]:
    """A socket mark per rule, in the order of the rules; the same rules get the same marks.

    The mark depends on the set of limited clients only, so a new speed keeps server.json, and
    xray need not restart for it.
    """
    emails = sorted({rule.email for rule in rules})
    mark_of = {email: _FIRST_MARK + index for index, email in enumerate(emails)}
    return {rule: mark_of[rule.email] for rule in sorted(rules)}


def speed_nft(rules: Iterable[XraySpeedRule]) -> str:
    """An `nft -f` file that replaces the table of the panel with `rules`; safe to load again."""
    marks = speed_marks(rules)
    lines = [
        "#!/usr/sbin/nft -f",
        "# Speed limits of Lamnezia keys. The panel rewrites this file; edits are lost.",
        f"table {_TABLE}",
        f"delete table {_TABLE}",
    ]
    if marks:
        out = [f"meta mark {mark} ct mark set meta mark" for mark in marks.values()]
        out += [f"meta mark {mark} {_police(rule)}" for rule, mark in marks.items()]
        incoming = [f"ct mark {mark} {_police(rule)}" for rule, mark in marks.items()]
        lines += [
            f"table {_TABLE} {{",
            "    chain out {",
            "        type filter hook output priority mangle; policy accept;",
            *(f"        {line}" for line in out),
            "    }",
            "    chain in {",
            "        type filter hook input priority filter; policy accept;",
            *(f"        {line}" for line in incoming),
            "    }",
            "}",
        ]
    return "\n".join(lines) + "\n"


def _police(rule: XraySpeedRule) -> str:
    return f"limit rate over {rule.rate} burst {rule.burst} drop"
