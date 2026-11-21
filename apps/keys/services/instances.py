"""Protocol instances keys can be issued on. The app that owns servers registers a provider."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass

from amnezia_core.awg_export import DEFAULT_MTU
from amnezia_core.containers import AWG2, WgContainer
from amnezia_core.ipsec import IpsecContainer
from amnezia_core.openvpn import OpenVpnContainer
from amnezia_core.vpn_link import DEFAULT_DNS
from amnezia_core.xray import XrayContainer
from apps.core.services.providers import Providers
from apps.ssh.services import RemoteHost


@dataclass(frozen=True)
class Instance:
    """`source` is the instance id shared with traffic; `host` and `port` are what clients use.

    `server_id` is the panel server the instance runs on, for the servers a plan allows.
    `dns` are the DNS servers and `mtu` the MTU new client profiles get. `short_id` is the
    Reality shortId of an XRay instance as the panel last read it from server.json; `server_ip`
    is the IP the IPsec server certificate is issued for. Both are empty for other protocols. An
    OpenVPN profile gets `openvpn_client_config` and `block_outside_dns` set on the settings
    page. New keys are not issued on a server under `maintenance`. `download_limit` and
    `upload_limit` are the speed limits of the whole instance in Mbit/s. `addresses` are the
    other checked addresses of the server a key may send clients to instead of `host`; none for
    keys of a server group, which go where the group sends them.
    """

    source: str
    title: str
    host: str
    connect: Callable[[], AbstractContextManager[RemoteHost]]
    container: WgContainer | XrayContainer | OpenVpnContainer | IpsecContainer = AWG2
    server_id: int | None = None
    dns: tuple[str, str] = DEFAULT_DNS
    mtu: str = DEFAULT_MTU
    short_id: str = ""
    server_ip: str = ""
    openvpn_client_config: str = ""
    block_outside_dns: bool = False
    maintenance: bool = False
    port: int | None = None
    download_limit: int | None = None
    upload_limit: int | None = None
    addresses: tuple[str, ...] = ()

    def client_host(self, address: str) -> str:
        """Where a key with the chosen `address` sends clients: an address no longer offered
        falls back to `host`."""
        return address if address in self.addresses else self.host


class UnknownInstanceError(LookupError):
    pass


class InstanceDirectory(Providers[Instance]):
    """Usage: ``@instances.register`` on a function that yields every instance."""

    def get(self, source: str) -> Instance:
        for instance in self:
            if instance.source == source:
                return instance
        raise UnknownInstanceError(source)

    def titles(self) -> dict[str, str]:
        return {instance.source: instance.title for instance in self}


instances = InstanceDirectory()
