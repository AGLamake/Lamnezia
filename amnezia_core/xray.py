"""XRay container of the Amnezia app: server.json, client profiles, vpn:// and vless:// links.

Mirrors upstream XrayServerConfig and XrayProtocolConfig (xrayProtocolConfig.cpp),
XrayConfigurator and ExportController::generateXrayConfig. Supported is what the app installs
by default: VLESS over raw TCP with Reality. Other transports and TLS are refused.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Any
from urllib.parse import quote

from amnezia_core.errors import ConfigFormatError, UnsupportedSetupError
from amnezia_core.qt_json import to_qt_compact_json, to_qt_json
from amnezia_core.vpn_link import DEFAULT_DNS, connection_config
from amnezia_core.xray_stats import STATS_BLOCKS, STATS_LEVEL, STATS_POLICY

XRAY_CONTAINER = "amnezia-xray"
XRAY_PROTOCOL = "xray"
XRAY_DIR = "/opt/amnezia/xray"
XRAY_CONFIG_PATH = f"{XRAY_DIR}/server.json"
XRAY_CLIENTS_TABLE_PATH = f"{XRAY_DIR}/clientsTable"
# Client of the app that installed the server; the app hides it from its client list.
XRAY_OWN_CLIENT_PATH = f"{XRAY_DIR}/xray_uuid.key"
XRAY_PRIVATE_KEY_PATH = f"{XRAY_DIR}/xray_private.key"
XRAY_PUBLIC_KEY_PATH = f"{XRAY_DIR}/xray_public.key"
XRAY_SHORT_ID_PATH = f"{XRAY_DIR}/xray_short_id.key"

DEFAULT_PORT = 443
DEFAULT_SITE = "www.googletagmanager.com"
DEFAULT_FLOW = "xtls-rprx-vision"
DEFAULT_FINGERPRINT = "chrome"
# XrayConfigModel::flowOptions and fingerprintOptions of the settings page of the app.
FLOWS = ("", "xtls-rprx-vision", "xtls-rprx-vision-udp443")
FINGERPRINTS = ("chrome", "firefox", "safari", "ios", "android", "edge", "360", "qq", "random")
# APPLICATION_NAME: the name the app puts after `#` in the vless:// links it exports.
VLESS_ALIAS = "AmneziaVPN"

_SETTINGS_VERSION = 2
_DEFAULT_ALPN = "h2"
_TRANSPORT_PROTO = "tcp"
_RAW_NETWORKS = ("tcp", "raw")
_REALITY = "reality"
_UDP443_SUFFIX = "-udp443"
_LEGACY_FINGERPRINT = "mozilla/5.0"
_LOCAL_PROXY_PORT = 10808
_LOCAL_LISTEN = "127.0.0.1"
_REALITY_DEST_PORT = 443
# Xray's value for no access log; the panel does not log the sites users visit.
_NO_ACCESS_LOG = "none"
_IPV4_DOTS = 3
# The characters the app lets into the site field, then NetworkUtilities::domainRegExp.
_SITE_CHARACTERS = re.compile(r"^[A-Za-z0-9._-]+$")
_DOMAIN = re.compile(
    r"(((?!-))(xn--)?[a-z0-9\-_]{0,61}[a-z0-9]\.)*(xn--)?([a-z0-9\-]{1,61}|[a-z0-9\-]{1,30})\.[a-z]{2,}",
    re.IGNORECASE,
)
_MAX_HOST_LENGTH = 253
# Outbounds the panel adds for speed limits: `lamnezia-<mark>`, one per limited client.
SPEED_OUTBOUND_PREFIX = "lamnezia-"
_DEFAULT_OUTBOUND: dict[str, Any] = {"protocol": "freedom"}


@dataclass(frozen=True)
class XrayContainer:
    """The XRay container of the Amnezia app; its files are at the XRAY_* paths."""

    name: str = XRAY_CONTAINER
    protocol: str = XRAY_PROTOCOL


XRAY = XrayContainer()


def client_fingerprint(client_id: str) -> str:
    """What identifies a client without its UUID, which is the secret of the client."""
    return hashlib.sha256(client_id.encode()).hexdigest()


def is_valid_site(site: str) -> bool:
    """A masking site the app accepts: XrayConfigModel::isValidSni, an IPv4 or a domain."""
    site = site.strip()
    if not site or len(site) > _MAX_HOST_LENGTH or not _SITE_CHARACTERS.match(site):
        return False
    if site.replace(".", "").isdigit() and site.count(".") == _IPV4_DOTS:
        try:
            IPv4Address(site)
        except ValueError:
            return False
        return True
    return not site.isdigit() and _DOMAIN.fullmatch(site) is not None


@dataclass(frozen=True)
class XraySettings:
    """Server half of a profile: upstream XrayServerConfig after applyDefaults()."""

    port: int = DEFAULT_PORT
    flow: str = DEFAULT_FLOW
    site: str = DEFAULT_SITE
    sni: str = DEFAULT_SITE
    fingerprint: str = DEFAULT_FINGERPRINT

    @property
    def server_flow(self) -> str:
        """Flow of server.json clients: xrayEffective::serverFlow drops the `-udp443` suffix."""
        return self.flow.removesuffix(_UDP443_SUFFIX)

    def to_json(self) -> dict[str, Any]:
        """XrayServerConfig::toJson for VLESS Reality over raw TCP."""
        data: dict[str, Any] = {
            "port": str(self.port),
            "transport_proto": _TRANSPORT_PROTO,
            "site": self.site,
            "xray_settings_version": _SETTINGS_VERSION,
            "xray_security": _REALITY,
            "xray_flow": self.flow,
            "xray_sni": self.sni,
            "xray_alpn": _DEFAULT_ALPN,
            "xray_fingerprint": self.fingerprint,
            "xray_transport": "raw",
        }
        return {key: value for key, value in data.items() if value != ""}


class XrayServerConfig:
    """server.json of the container. The panel edits the client list, the statistics blocks
    (`enable_stats`) and the routes of speed-limited clients (`set_speed_marks`); the rest is
    kept as read.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        inbounds = data.get("inbounds")
        if not isinstance(inbounds, list) or not inbounds or not isinstance(inbounds[0], dict):
            raise ConfigFormatError("server.json has no inbounds.")
        settings = inbounds[0].get("settings")
        if not isinstance(settings, dict) or not isinstance(settings.get("clients"), list):
            raise ConfigFormatError("server.json has no client list in the first inbound.")
        self._data = data

    @classmethod
    def for_setup(
        cls, settings: XraySettings, *, own_client_id: str, private_key: str, short_id: str
    ) -> "XrayServerConfig":
        """server.json of a new install: XrayServerConfig::toServerInboundJson, one client.

        Unlike the app, the access log is off: by default Xray prints every destination to
        stdout, and with the container log on it would stay in `docker logs`.
        """
        reality = {
            "dest": f"{settings.site}:{_REALITY_DEST_PORT}",
            "privateKey": private_key,
            "serverNames": [settings.sni or settings.site],
            "shortIds": [short_id],
        }
        inbound = {
            "port": settings.port,
            "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "streamSettings": {
                "network": _TRANSPORT_PROTO,
                "security": _REALITY,
                "realitySettings": reality,
            },
        }
        config = cls(
            {
                "log": {"loglevel": "error", "access": _NO_ACCESS_LOG},
                "inbounds": [inbound],
                "outbounds": [{"protocol": "freedom"}],
            }
        )
        config.add_client(own_client_id, settings.server_flow)
        config.enable_stats()
        return config

    @classmethod
    def parse(cls, text: str) -> "XrayServerConfig":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as error:
            raise ConfigFormatError(f"server.json is not valid JSON: {error}") from error
        if not isinstance(data, dict):
            raise ConfigFormatError("server.json must be a JSON object.")
        return cls(data)

    def serialize(self) -> str:
        """Indented, as the app writes it with QJsonDocument::toJson."""
        return to_qt_json(self._data)

    @property
    def client_ids(self) -> list[str]:
        return [client["id"] for client in self._clients if isinstance(client.get("id"), str)]

    def add_client(self, client_id: str, flow: str) -> None:
        """Append `{"id", "flow"}` like XrayServerConfig::makeClientEntry."""
        if client_id in self.client_ids:
            raise ConfigFormatError(f"Client {client_id} is already in server.json.")
        entry = {"id": client_id, "flow": flow} if flow else {"id": client_id}
        self._clients.append(entry)

    def remove_client(self, client_id: str) -> None:
        self._clients[:] = [client for client in self._clients if client.get("id") != client_id]

    @property
    def stats_enabled(self) -> bool:
        """Whether xray counts traffic of every client and answers `STATS_COMMAND`."""
        level = _object(_object(_object(self._data, "policy"), "levels"), STATS_LEVEL)
        return (
            all(self._data.get(name) == block for name, block in STATS_BLOCKS.items())
            and all(level.get(flag) is True for flag in STATS_POLICY)
            and all(client.get("email") == _email(client) for client in self._clients)
        )

    def enable_stats(self) -> None:
        """Add the `stats`, `api` and `policy` blocks and an `email` to every client.

        The email is the fingerprint of the client UUID: counters are named by it, and the UUID,
        the client's credential, never shows in them. An existing policy keeps its other levels
        and flags. The app drops the blocks when it rebuilds server.json; emails stay.
        """
        self._data.update(deepcopy(STATS_BLOCKS))
        levels = _object(_object(self._data, "policy", create=True), "levels", create=True)
        _object(levels, STATS_LEVEL, create=True).update(STATS_POLICY)
        for client in self._clients:
            if email := _email(client):
                client["email"] = email

    def set_speed_marks(self, marks: Mapping[str, int]) -> None:
        """Route the traffic of each client (by `email`) out with its socket mark.

        Each mark gets a `freedom` outbound tagged `lamnezia-<mark>` and a routing rule by
        `user`; the routes of the previous call are replaced, so `{}` takes them all out and
        leaves the config as the app wrote it. The first outbound stays the default for the
        other clients. Rules go after the existing ones, which keep deciding first. The app
        drops all of it when it rebuilds server.json.
        """
        outbounds = [o for o in _list(self._data, "outbounds") if not _is_speed_route(o, "tag")]
        routing = _object(self._data, "routing")
        rules = [r for r in _list(routing, "rules") if not _is_speed_route(r, "outboundTag")]
        if marks and not outbounds:
            outbounds.append(deepcopy(_DEFAULT_OUTBOUND))
        for email, mark in sorted(marks.items(), key=lambda item: item[1]):
            tag = f"{SPEED_OUTBOUND_PREFIX}{mark}"
            outbounds.append(
                {"tag": tag, "protocol": "freedom", "streamSettings": {"sockopt": {"mark": mark}}}
            )
            rules.append({"type": "field", "user": [email], "outboundTag": tag})
        if outbounds or "outbounds" in self._data:
            self._data["outbounds"] = outbounds
        if rules:
            routing["rules"] = rules
            self._data["routing"] = routing
        elif "rules" in routing:
            del routing["rules"]
            if not routing:
                del self._data["routing"]

    def settings(self) -> XraySettings:
        """Settings as XrayServerConfig::fromServerInboundJson reads them; Reality over TCP only."""
        inbound = self._inbound
        stream = inbound.get("streamSettings")
        if not isinstance(stream, dict):
            raise ConfigFormatError("server.json has no streamSettings in the first inbound.")
        network = stream.get("network", "tcp")
        security = stream.get("security", "none")
        if network not in _RAW_NETWORKS or security != _REALITY:
            raise UnsupportedSetupError(f"XRay {network} with {security} is not supported.")
        reality = stream.get("realitySettings")
        reality = reality if isinstance(reality, dict) else {}
        site = _host_of(str(reality.get("dest", "")))
        names = reality.get("serverNames")
        sni = names[0] if isinstance(names, list) and names else reality.get("serverName", "")
        # The flow lives in the client entries; upstream takes it from the first one.
        flow = str(self._clients[0].get("flow", "")) if self._clients else DEFAULT_FLOW
        return XraySettings(
            port=int(inbound.get("port", DEFAULT_PORT)),
            flow=flow,
            site=site or sni or DEFAULT_SITE,
            sni=sni or site or DEFAULT_SITE,
            fingerprint=_normalized_fingerprint(str(reality.get("fingerprint", ""))),
        )

    def apply_settings(self, settings: XraySettings) -> None:
        """The port, the masking site, the SNI and the flow of `settings`.

        As the app's change of the server settings writes them (serverView,
        applyFlowToClients), but the clients keep their emails and the statistics blocks and
        the speed routes stay. The fingerprint is the clients' alone: server.json does not
        have it. Reality over TCP only, as `settings()` reads it.
        """
        self.settings()
        self._inbound["port"] = settings.port
        reality = self._reality
        reality["dest"] = f"{settings.site}:{_REALITY_DEST_PORT}"
        reality["serverNames"] = [settings.sni or settings.site]
        reality.pop("serverName", None)
        for client in self._clients:
            if settings.server_flow:
                client["flow"] = settings.server_flow
            else:
                client.pop("flow", None)

    @property
    def short_id(self) -> str:
        """The first Reality shortId, which the app hands to clients."""
        ids = self._reality.get("shortIds")
        return str(ids[0]) if isinstance(ids, list) and ids else ""

    @property
    def private_key(self) -> str:
        """The Reality private key of the server."""
        return str(self._reality.get("privateKey") or "")

    @property
    def _reality(self) -> dict[str, Any]:
        stream = self._inbound.get("streamSettings")
        reality = stream.get("realitySettings") if isinstance(stream, dict) else None
        return reality if isinstance(reality, dict) else {}

    @property
    def _inbound(self) -> dict[str, Any]:
        inbound: dict[str, Any] = self._data["inbounds"][0]
        return inbound

    @property
    def _clients(self) -> list[dict[str, Any]]:
        clients: list[dict[str, Any]] = self._inbound["settings"]["clients"]
        return clients


@dataclass(frozen=True, repr=False)
class XrayClientProfile:
    """Everything needed to hand an XRay client to a user: native JSON, vpn://, vless://.

    `client_id` is the VLESS UUID, the user's credential; `repr` hides it.
    """

    host: str
    server: XraySettings
    client_id: str
    public_key: str
    short_id: str

    def __repr__(self) -> str:
        return f"XrayClientProfile(host={self.host!r})"

    def native_config(self) -> dict[str, Any]:
        """XrayProtocolConfig::toClientOutboundJson: local SOCKS inbound, VLESS Reality outbound."""
        user = {"id": self.client_id, "encryption": "none"}
        if self.server.flow:
            user["flow"] = self.server.flow
        reality = {
            "fingerprint": self.server.fingerprint,
            "serverName": self.server.sni,
            "publicKey": self.public_key,
            "shortId": self.short_id,
            "spiderX": "",
        }
        outbound = {
            "protocol": "vless",
            "settings": {
                "vnext": [{"address": self.host, "port": self.server.port, "users": [user]}]
            },
            "streamSettings": {"network": "tcp", "security": _REALITY, "realitySettings": reality},
        }
        inbound = {
            "listen": _LOCAL_LISTEN,
            "port": _LOCAL_PROXY_PORT,
            "protocol": "socks",
            "settings": {"udp": True},
        }
        return {"log": {"loglevel": "error"}, "inbounds": [inbound], "outbounds": [outbound]}

    def vpn_config(
        self, description: str = "", dns: tuple[str, str] = DEFAULT_DNS
    ) -> dict[str, Any]:
        """Server JSON for vpn://. `last_config` is the native JSON itself, as the app stores it."""
        container = {
            "container": XRAY_CONTAINER,
            XRAY_PROTOCOL: {
                **self.server.to_json(),
                "last_config": to_qt_compact_json(self.native_config()),
            },
        }
        return connection_config(self.host, container, description, dns)

    def vless_link(self, alias: str = VLESS_ALIAS) -> str:
        """`vless://` as ExportController::generateXrayConfig and vless::Serialize build it."""
        query = {
            "encryption": "none",
            "security": _REALITY,
            # The export falls back to the default flow when the client has none.
            "flow": self.server.flow or DEFAULT_FLOW,
            "sni": self.server.sni,
            "fp": self.server.fingerprint,
            "pbk": self.public_key,
            "sid": self.short_id,
        }
        params = "&".join(f"{key}={_encode(value)}" for key, value in query.items() if value)
        address = f"{_encode(self.client_id)}@{_uri_host(self.host)}:{self.server.port}"
        return f"vless://{address}?{params}#{_encode(alias)}"


def _uri_host(host: str) -> str:
    """The host as a URI holds it: QUrl::setHost puts an IPv6 address in brackets."""
    try:
        is_ipv6 = isinstance(ip_address(host), IPv6Address)
    except ValueError:
        is_ipv6 = False
    return f"[{host}]" if is_ipv6 else host


def _object(parent: dict[str, Any], key: str, *, create: bool = False) -> dict[str, Any]:
    """`parent[key]` if it is an object; otherwise an empty one, put in its place on `create`."""
    value = parent.get(key)
    if isinstance(value, dict):
        return value
    value = {}
    if create:
        parent[key] = value
    return value


def _list(parent: dict[str, Any], key: str) -> list[Any]:
    value = parent.get(key)
    return value if isinstance(value, list) else []


def _is_speed_route(entry: object, tag_key: str) -> bool:
    """Whether an outbound (`tag`) or a routing rule (`outboundTag`) is a speed route."""
    tag = entry.get(tag_key) if isinstance(entry, dict) else None
    return isinstance(tag, str) and tag.startswith(SPEED_OUTBOUND_PREFIX)


def _email(client: dict[str, Any]) -> str | None:
    client_id = client.get("id")
    return client_fingerprint(client_id) if isinstance(client_id, str) else None


def _host_of(dest: str) -> str:
    """hostFromDest: `site:443` -> `site`."""
    host, colon, _port = dest.strip().rpartition(":")
    return host if colon and host else dest.strip()


def _normalized_fingerprint(fingerprint: str) -> str:
    if not fingerprint or _LEGACY_FINGERPRINT in fingerprint.lower():
        return DEFAULT_FINGERPRINT
    return fingerprint


def _encode(value: str) -> str:
    return quote(value, safe="")
