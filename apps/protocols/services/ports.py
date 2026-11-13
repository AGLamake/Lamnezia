"""Ports of new containers: AmneziaWG gets a free port up to 9999, where providers let UDP pass.

Some providers block or throttle UDP above 9999, and the Amnezia documentation recommends a
port up to 9999. The Amnezia app uses 55424 for both AmneziaWG versions, so the second one on a
server needed a port by hand; the panel picks the first free port of its own list instead.
"""

from collections.abc import Iterable

from django.utils.translation import get_language

LOW_PORT_LIMIT = 9999
# Not a well-known UDP port (1194 OpenVPN, 3478 STUN, 4500 IPsec, 5060 SIP, 5353 mDNS).
_FIRST_LOW_PORT = 5542
LOW_PORTS = (*range(_FIRST_LOW_PORT, LOW_PORT_LIMIT + 1), *range(1024, _FIRST_LOW_PORT))
WIKI_URL = "https://github.com/AGLamake/Lamnezia/wiki"
PORTS_PAGE = "Ports"

type Taken = set[tuple[int, str]]


def is_high(port: int | None) -> bool:
    """A port some providers block or throttle UDP on."""
    return port is not None and port > LOW_PORT_LIMIT


def free_ports(
    candidates: Iterable[int], taken: Taken, transports: tuple[str, ...], count: int = 1
) -> list[int]:
    """The first `count` candidates free for every transport; fewer if the candidates run out."""
    found: list[int] = []
    for port in candidates:
        if all((port, transport) not in taken for transport in transports):
            found.append(port)
            if len(found) == count:
                break
    return found


def ports_page_url() -> str:
    """The wiki page on choosing the port and testing the speed, in the language of the page."""
    prefix = "ru-" if (get_language() or "").startswith("ru") else ""
    return f"{WIKI_URL}/{prefix}{PORTS_PAGE}"
