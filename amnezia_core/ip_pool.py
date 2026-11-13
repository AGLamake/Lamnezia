"""Choosing a tunnel address for a new client in the server subnet."""

from collections.abc import Iterable
from ipaddress import IPv4Address, IPv4Interface

from amnezia_core.errors import SubnetExhaustedError

# The app never hands out addresses ending in .0 or .255, even inside wider subnets.
_SKIPPED_LAST_OCTETS = (0, 255)


class IpPool:
    """Free addresses of the subnet, compatible with how the Amnezia app picks them.

    The app takes the address of the last peer in awg0.conf plus one. The panel appends
    peers at the end too, so it hands out the address after the highest one in use; this
    keeps the app's next choice free. Gaps are reused only when the subnet top is taken.
    """

    def __init__(self, server: IPv4Interface, used: Iterable[IPv4Address]) -> None:
        self._network = server.network
        self._taken = {server.ip, *used}

    def next_address(self) -> IPv4Address:
        free = [
            host
            for host in self._network.hosts()
            if host not in self._taken and host.packed[-1] not in _SKIPPED_LAST_OCTETS
        ]
        if not free:
            raise SubnetExhaustedError(f"No free addresses left in {self._network}.")
        highest = max(self._taken)
        return next((host for host in free if host > highest), free[0])
