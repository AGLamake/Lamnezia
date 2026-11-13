"""Protocol instances keys can be issued on. The app that owns servers registers a provider."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass

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
    `dns` are the DNS servers new client profiles get. `short_id` is the Reality shortId of an
    XRay instance as the panel last read it from server.json; `server_ip` is the IP the IPsec
    server certificate is issued for. Both are empty for other protocols. An OpenVPN profile
    gets `openvpn_client_config` and `block_outside_dns` set on the settings page. New keys are
    not issued on a server under `maintenance`.
    """

    source: str
    title: str
    host: str
    connect: Callable[[], AbstractContextManager[RemoteHost]]
    container: WgContainer | XrayContainer | OpenVpnContainer | IpsecContainer = AWG2
    server_id: int | None = None
    dns: tuple[str, str] = DEFAULT_DNS
    short_id: str = ""
    server_ip: str = ""
    openvpn_client_config: str = ""
    block_outside_dns: bool = False
    maintenance: bool = False
    port: int | None = None


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
