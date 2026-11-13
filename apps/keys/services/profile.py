"""What the panel keeps to hand an issued key out again: vpn://, QR, files, vless://."""

import json
from dataclasses import asdict, dataclass, replace
from ipaddress import IPv4Address, IPv4Interface
from typing import Any

from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from amnezia_core.awg_export import AwgClientProfile, AwgServerSettings
from amnezia_core.containers import WG_CONTAINERS, WgContainer
from amnezia_core.ipsec import IPSEC_CONTAINER
from amnezia_core.ipsec_profile import IpsecClientProfile
from amnezia_core.keys import KeyPair
from amnezia_core.openvpn import OPENVPN_CONTAINER, OpenVpnClientProfile, OpenVpnSettings
from amnezia_core.qr import qr_png, qr_svg
from amnezia_core.vpn_link import DEFAULT_DNS, encode_vpn_link, qr_payloads
from amnezia_core.xray import XRAY_CONTAINER, XrayClientProfile, XraySettings
from apps.keys.models import AccessKey

_TEXT = "text/plain"


@dataclass(frozen=True)
class ServerProfile:
    """Server half of a client profile as it was when the key was issued or last updated."""

    container: str
    host: str
    port: int
    subnet: str
    params: dict[str, str]
    server_public_key: str
    preshared_key: str
    dns: tuple[str, str] = DEFAULT_DNS

    @classmethod
    def build(
        cls,
        container: WgContainer,
        host: str,
        server: AwgServerSettings,
        server_public_key: str,
        preshared_key: str,
    ) -> "ServerProfile":
        return cls(
            container.name,
            host,
            server.port,
            str(server.subnet),
            dict(server.params),
            server_public_key,
            preshared_key,
        )

    def updated(
        self, port: int, params: dict[str, str], dns: tuple[str, str], subnet: str = ""
    ) -> "ServerProfile":
        """The same peer after the protocol settings of its server were changed."""
        subnet = subnet or self.subnet
        return replace(self, port=port, params=dict(params), dns=dns, subnet=subnet)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "ServerProfile":
        data: dict[str, Any] = json.loads(text)
        # Profiles saved before DNS was configurable have none: the app default applies.
        data["dns"] = tuple(data.get("dns", DEFAULT_DNS))
        return cls(**data)


@dataclass(frozen=True)
class XrayKeyProfile:
    """Server half of an XRay client profile when the key was issued; the UUID is the key's own."""

    host: str
    port: int
    flow: str
    site: str
    sni: str
    fingerprint: str
    server_public_key: str
    short_id: str
    dns: tuple[str, str] = DEFAULT_DNS
    container: str = XRAY_CONTAINER

    @classmethod
    def build(
        cls, host: str, server: XraySettings, server_public_key: str, short_id: str
    ) -> "XrayKeyProfile":
        return cls(
            host,
            server.port,
            server.flow,
            server.site,
            server.sni,
            server.fingerprint,
            server_public_key,
            short_id,
        )

    def client(self, client_id: str) -> XrayClientProfile:
        server = XraySettings(self.port, self.flow, self.site, self.sni, self.fingerprint)
        return XrayClientProfile(
            self.host, server, client_id, self.server_public_key, self.short_id
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "XrayKeyProfile":
        data: dict[str, Any] = json.loads(text)
        data["dns"] = tuple(data["dns"])
        return cls(**data)


@dataclass(frozen=True, repr=False)
class OpenVpnKeyProfile:
    """Server half of an OpenVPN client profile: settings, CA, the signed certificate, ta.key.

    The private key of the client is the key's own; ta.key is a server secret, so `repr`
    leaves it out (the whole profile is stored encrypted).
    """

    host: str
    server: dict[str, Any]
    ca_cert: str
    client_cert: str
    ta_key: str
    dns: tuple[str, str] = DEFAULT_DNS
    container: str = OPENVPN_CONTAINER
    block_outside_dns: bool = False

    def __repr__(self) -> str:
        return f"OpenVpnKeyProfile(host={self.host!r}, server={self.server!r})"

    @classmethod
    def build(
        cls, host: str, server: OpenVpnSettings, ca_cert: str, client_cert: str, ta_key: str
    ) -> "OpenVpnKeyProfile":
        return cls(host, server.to_json(), ca_cert, client_cert, ta_key)

    def client(self, client_id: str, private_key: str) -> OpenVpnClientProfile:
        server = OpenVpnSettings.from_json(self.server)
        return OpenVpnClientProfile(
            self.host,
            server,
            client_id,
            private_key,
            self.ca_cert,
            self.client_cert,
            self.ta_key,
            self.block_outside_dns,
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "OpenVpnKeyProfile":
        data: dict[str, Any] = json.loads(text)
        data["dns"] = tuple(data["dns"])
        return cls(**data)


def profile_container(text: str) -> str:
    """Container of a stored profile; WireGuard-family profiles name theirs too."""
    return str(json.loads(text).get("container", "")) if text else ""


@dataclass(frozen=True)
class IpsecKeyProfile:
    """Server half of an IKEv2 client profile: the CA and the identity of the server.

    The PKCS#12 with the client's certificate and key is the key's own private key.
    """

    host: str
    server_ip: str
    ca_cert: str
    dns: tuple[str, str] = DEFAULT_DNS
    container: str = IPSEC_CONTAINER

    def client(self, client_id: str, p12: str) -> IpsecClientProfile:
        return IpsecClientProfile(self.host, self.server_ip, client_id, p12, self.ca_cert)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "IpsecKeyProfile":
        data: dict[str, Any] = json.loads(text)
        data["dns"] = tuple(data["dns"])
        return cls(**data)


@dataclass(frozen=True, repr=False)
class ExportFile:
    """A file the user opens in a VPN app; it holds the key's secrets, so `repr` hides it."""

    extension: str
    content: str
    content_type: str
    label: str | Promise

    def __repr__(self) -> str:
        return f"ExportFile(extension={self.extension!r})"


@dataclass(frozen=True, repr=False)
class KeyExport:
    """The ways to give a key to its owner. Only keys issued by the panel can be exported.

    `files` are the .conf of WireGuard-family keys, the .ovpn of OpenVPN ones or the
    strongSwan and Apple profiles of IPsec ones; `vless_link` is for XRay keys. All of them
    hold secrets.
    """

    vpn_link: str
    files: tuple[ExportFile, ...] = ()
    vless_link: str | None = None

    def file(self, extension: str) -> ExportFile | None:
        return next((file for file in self.files if file.extension == extension), None)

    @classmethod
    def of(cls, key: AccessKey) -> "KeyExport":
        if not key.can_export:
            raise ValueError("this key cannot be exported")
        container = profile_container(key.profile)
        if container == XRAY_CONTAINER:
            xray = XrayKeyProfile.from_json(key.profile)
            client = xray.client(key.private_key)
            vpn_config = client.vpn_config(key.name, xray.dns)
            return cls(encode_vpn_link(vpn_config), vless_link=client.vless_link())
        if container == OPENVPN_CONTAINER:
            openvpn = OpenVpnKeyProfile.from_json(key.profile)
            vpn = openvpn.client(key.public_key, key.private_key)
            ovpn = ExportFile(
                "ovpn", vpn.ovpn_file(openvpn.dns), _TEXT, _("Download .ovpn for OpenVPN")
            )
            return cls(encode_vpn_link(vpn.vpn_config(key.name, openvpn.dns)), (ovpn,))
        if container == IPSEC_CONTAINER:
            return cls._ipsec(key)
        return cls._wireguard(key)

    @classmethod
    def _ipsec(cls, key: AccessKey) -> "KeyExport":
        ipsec = IpsecKeyProfile.from_json(key.profile)
        client = ipsec.client(key.public_key, key.private_key)
        files = (
            ExportFile(
                "sswan",
                client.sswan_file(),
                "application/vnd.strongswan.profile",
                _("Download .sswan for strongSwan on Android"),
            ),
            ExportFile(
                "mobileconfig",
                client.mobileconfig_file(),
                "application/x-apple-aspen-config",
                _("Download .mobileconfig for iPhone, iPad and Mac"),
            ),
        )
        return cls(encode_vpn_link(client.vpn_config(key.name, ipsec.dns)), files)

    @classmethod
    def _wireguard(cls, key: AccessKey) -> "KeyExport":
        if key.client_ip is None:
            raise ValueError("this key cannot be exported")
        server = ServerProfile.from_json(key.profile)
        profile = AwgClientProfile(
            host=server.host,
            server=AwgServerSettings(server.port, IPv4Interface(server.subnet), server.params),
            server_public_key=server.server_public_key,
            preshared_key=server.preshared_key,
            keys=KeyPair.from_private_key(key.private_key),
            client_ip=IPv4Address(key.client_ip),
            container=WG_CONTAINERS[server.container],
        )
        conf = ExportFile(
            "conf", profile.conf_file(server.dns), _TEXT, _("Download .conf for AmneziaWG")
        )
        return cls(encode_vpn_link(profile.vpn_config(key.name, server.dns)), (conf,))

    def qr_codes(self) -> list[str]:
        """SVG of every QR code in the series the Amnezia app scans."""
        return [qr_svg(payload) for payload in qr_payloads(self.vpn_link)]

    def qr_images(self) -> list[bytes]:
        """The same series as PNG images, for the bots."""
        return [qr_png(payload) for payload in qr_payloads(self.vpn_link)]
