"""Traffic statistics of XRay clients: the server.json blocks that turn them on and `statsquery`.

Xray counts traffic per client only for clients with an `email`, and only with the `stats`,
`api` and `policy` blocks in its config. The Amnezia app writes none of them; the panel adds
them (docs/DESIGN.md, 3.9). The counters live in the memory of xray: they start
from zero whenever the container restarts.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from amnezia_core.errors import DumpFormatError

# The API listens only inside the container, which has its own network namespace.
STATS_API = "127.0.0.1:10085"
STATS_BLOCKS: dict[str, Any] = {
    "stats": {},
    "api": {"tag": "api", "listen": STATS_API, "services": ["StatsService"]},
}
STATS_LEVEL = "0"
STATS_POLICY = {"statsUserUplink": True, "statsUserDownlink": True}
STATS_COMMAND = f'xray api statsquery --server={STATS_API} -pattern "user>>>"'

_PREFIX = "user"
_TRAFFIC = "traffic"
_UPLINK = "uplink"
_DOWNLINK = "downlink"
_NAME_PARTS = 4


@dataclass(frozen=True)
class XrayClientTraffic:
    """Counters of one client since xray started. `rx_bytes` is client upload.

    `public_key` is the client's `email`, which the panel sets to the fingerprint of its UUID,
    so the counters are found the way the key is. Xray has no handshakes.
    """

    public_key: str
    rx_bytes: int = 0
    tx_bytes: int = 0
    latest_handshake: datetime | None = None


def parse_stats(text: str) -> list[XrayClientTraffic]:
    """Clients in the output of `STATS_COMMAND`.

    `{}` means no counters yet: a client appears after its first connection. A counter at zero
    comes without `value`.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise DumpFormatError(f"statsquery output is not JSON: {error}") from error
    stats = data.get("stat", []) if isinstance(data, dict) else None
    if not isinstance(stats, list):
        raise DumpFormatError("statsquery output has no stat list.")
    counters: dict[str, dict[str, int]] = {}
    for item in stats:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise DumpFormatError("statsquery entry has no name.")
        parts = item["name"].split(">>>")
        if len(parts) != _NAME_PARTS or parts[0] != _PREFIX or parts[2] != _TRAFFIC:
            continue
        counters.setdefault(parts[1], {})[parts[3]] = _value(item)
    return [
        XrayClientTraffic(email, values.get(_UPLINK, 0), values.get(_DOWNLINK, 0))
        for email, values in counters.items()
    ]


def _value(item: dict[str, Any]) -> int:
    value = item.get("value", 0)
    # Protobuf JSON may print int64 as a string.
    if isinstance(value, str) and value.isdigit():
        return int(value)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise DumpFormatError(f"statsquery value is not a counter: {value!r}")
