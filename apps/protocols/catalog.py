"""Names, groups and short descriptions of the containers the panel manages.

They are the ones the Amnezia app shows. Follows upstream ContainerUtils: containerHumanNames,
containerDescriptions and the service type that splits the app's list into VPN protocols and
services.
"""

from dataclasses import dataclass

from django.db import models
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from amnezia_core.containers import AWG2, AWG_LEGACY, WIREGUARD
from amnezia_core.ipsec import IPSEC_CONTAINER
from amnezia_core.openvpn import OPENVPN_CONTAINER
from amnezia_core.proxies import (
    MTPROXY_CONTAINER,
    SOCKS5_CONTAINER,
    TELEMT_CONTAINER,
    TPROXY_CONTAINER,
)
from amnezia_core.services import DNS_CONTAINER, SFTP_CONTAINER, TOR_CONTAINER
from amnezia_core.xray import XRAY_CONTAINER


class ContainerGroup(models.TextChoices):
    VPN = "vpn", _("VPN protocols")
    SERVICES = "services", _("Services")


@dataclass(frozen=True)
class ContainerKind:
    """What a container is for: its name, its group and one or two sentences about it."""

    title: str | Promise
    group: ContainerGroup
    description: Promise


_AWG = _(
    "AmneziaWG is a special protocol from Amnezia based on WireGuard. It provides high "
    "connection speed and ensures stable operation even in the most challenging network "
    "conditions."
)

CATALOG: dict[str, ContainerKind] = {
    AWG2.name: ContainerKind("AmneziaWG 2.0", ContainerGroup.VPN, _AWG),
    AWG_LEGACY.name: ContainerKind(_("AmneziaWG (legacy)"), ContainerGroup.VPN, _AWG),
    WIREGUARD.name: ContainerKind(
        "WireGuard",
        ContainerGroup.VPN,
        _(
            "WireGuard - popular VPN protocol with high performance, high speed and low power "
            "consumption."
        ),
    ),
    XRAY_CONTAINER: ContainerKind(
        "XRay (VLESS Reality)",
        ContainerGroup.VPN,
        _(
            "XRay with REALITY masks VPN traffic as web traffic and protects against active "
            "probing. It is highly resistant to detection and offers high speed."
        ),
    ),
    OPENVPN_CONTAINER: ContainerKind(
        "OpenVPN",
        ContainerGroup.VPN,
        _(
            "OpenVPN is the most popular VPN protocol, with flexible configuration options. It "
            "uses its own security protocol with SSL/TLS for key exchange."
        ),
    ),
    IPSEC_CONTAINER: ContainerKind(
        "IPsec",
        ContainerGroup.VPN,
        _(
            "IKEv2/IPsec - modern stable protocol, a bit faster than others, restores connection "
            "after signal loss. It has native support on the latest versions of Android and iOS."
        ),
    ),
    TOR_CONTAINER: ContainerKind(
        _("Website in Tor network"),
        ContainerGroup.SERVICES,
        _("Deploy a WordPress site on the Tor network in two clicks."),
    ),
    DNS_CONTAINER: ContainerKind(
        "AmneziaDNS",
        ContainerGroup.SERVICES,
        _("Replace the current DNS server with your own. This will increase your privacy level."),
    ),
    SFTP_CONTAINER: ContainerKind(
        _("SFTP file sharing"),
        ContainerGroup.SERVICES,
        _("Create a file vault on your server to securely store and transfer files."),
    ),
    SOCKS5_CONTAINER: ContainerKind(
        _("SOCKS5 proxy server"),
        ContainerGroup.SERVICES,
        _("A proxy for apps and browsers that support SOCKS5."),
    ),
    MTPROXY_CONTAINER: ContainerKind(
        "MTProxy (Telegram)", ContainerGroup.SERVICES, _("Telegram MTProto proxy server")
    ),
    TELEMT_CONTAINER: ContainerKind(
        "Telemt (Telegram)", ContainerGroup.SERVICES, _("Telegram MTProto proxy (Telemt, Rust)")
    ),
    TPROXY_CONTAINER: ContainerKind(
        "TProxy (Telegram WEB)", ContainerGroup.SERVICES, _("Telegram WEB proxy (tproxy-server)")
    ),
}


def titles(group: ContainerGroup) -> dict[str, str | Promise]:
    """Container name → the name the panel shows, for one group."""
    return {name: kind.title for name, kind in CATALOG.items() if kind.group == group}
