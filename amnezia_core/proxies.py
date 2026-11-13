"""The proxies of the Amnezia app: SOCKS5, MTProxy, Telemt and TProxy (Telegram WEB).

Follows upstream server_scripts/{socks5_proxy,mtproxy,telemt,tproxy}/, their installers in
client/core/installers/ and the links of PageServiceMtProtoSettingsBase.qml. The app keeps no
file of its own for them: it reads what configure_container.sh leaves in the container.
"""

import re
import secrets
import string
from dataclasses import dataclass
from urllib.parse import urlencode

from amnezia_core.errors import ConfigFormatError

SOCKS5_CONTAINER = "amnezia-socks5proxy"
MTPROXY_CONTAINER = "amnezia-mtproxy"
TELEMT_CONTAINER = "amnezia-telemt"
TPROXY_CONTAINER = "amnezia-tproxy"
# The proxies of Telegram: the app shows their tg:// and t.me links.
TELEGRAM_PROXIES = (MTPROXY_CONTAINER, TELEMT_CONTAINER, TPROXY_CONTAINER)

SOCKS5_PORT = 38080
SOCKS5_USER = "proxy_user"
SOCKS5_CONFIG_PATH = "/usr/local/3proxy/conf/3proxy.cfg"
# The `log` line of upstream configure_container.sh: every host users ask for. The panel writes
# 3proxy.cfg without it; a proxy set up in the app keeps the file.
SOCKS5_LOG_PATH = "/usr/local/3proxy/logs/3proxy.log"
# Utils::getRandomString(16) of Socks5Installer::generateConfig.
SOCKS5_PASSWORD_LENGTH = 16
_PASSWORD_ALPHABET = string.ascii_letters + string.digits
# Socks5Installer::extractConfigFromContainer.
_SOCKS5_USERS = re.compile(r"users (\w+):CL:(\w+)")

MTPROXY_PORT = 443
TELEMT_PORT = 443
TELEMT_USER = "amnezia"
TPROXY_PORT = 443
# Caddy answers the ACME challenge on it; the panel publishes it as the app does by default.
TPROXY_HTTP_PORT = 80

STANDARD = "standard"
FAKETLS = "faketls"
DEFAULT_TLS_DOMAIN = "googletagmanager.com"

SECRET_PATH = "/data/secret"
MTPROXY_META_PATH = "/data/mtproxy-meta"
TELEMT_CONFIG_PATH = "/data/config.toml"
TPROXY_META_PATH = "/data/tproxy-meta"

_SECRET = re.compile(r"[0-9a-fA-F]{32}")
# The marker configure_container.sh prints, as InstallController reads it after the install.
_CONFIGURE_SECRET = re.compile(r"\[\*\]\s+Secret:\s+([0-9a-fA-F]{32})")


@dataclass(frozen=True, repr=False)
class Socks5Account:
    """The account of amnezia-socks5proxy; `repr` hides the password, a secret."""

    user: str
    password: str

    @classmethod
    def generate(cls) -> "Socks5Account":
        password = "".join(
            secrets.choice(_PASSWORD_ALPHABET) for _ in range(SOCKS5_PASSWORD_LENGTH)
        )
        return cls(SOCKS5_USER, password)

    @classmethod
    def from_config(cls, text: str) -> "Socks5Account | None":
        """The account in 3proxy.cfg; None for a proxy without a password (`auth none`)."""
        match = _SOCKS5_USERS.search(text)
        return cls(match[1], match[2]) if match else None

    @property
    def users_line(self) -> str:
        """`$SOCKS5_USER` of configure_container.sh, as genSocks5ProxyVars makes it."""
        return f"users {self.user}:CL:{self.password}"


@dataclass(frozen=True)
class ProxyLinks:
    """The links Telegram opens to add the proxy."""

    tg: str
    tme: str


def proxy_secret(text: str) -> str:
    """The secret in /data/secret; empty if it is not 32 hex digits, which the app ignores."""
    secret = text.strip()
    return secret if _SECRET.fullmatch(secret) else ""


def configure_secret(output: str) -> str:
    """The secret configure_container.sh of a Telegram proxy prints when it is done."""
    match = _CONFIGURE_SECRET.search(output)
    if not match:
        raise ConfigFormatError("configure_container.sh printed no proxy secret")
    return match[1]


def client_secret(secret: str, mode: str, tls_domain: str) -> str:
    """The secret of the link: `dd` and the secret, or `ee`, the secret and the TLS domain."""
    if mode == FAKETLS:
        return f"ee{secret}{(tls_domain or DEFAULT_TLS_DOMAIN).encode().hex()}"
    return f"dd{secret}"


def mtproto_links(host: str, port: int, secret: str) -> ProxyLinks:
    """Links of MTProxy and Telemt; `secret` is the one of the link (`client_secret`)."""
    query = urlencode({"server": host, "port": port, "secret": secret})
    return ProxyLinks(f"tg://proxy?{query}", f"https://t.me/proxy?{query}")


def web_proxy_links(hostname: str, secret: str) -> ProxyLinks:
    """Links of TProxy: HTTPS on the domain of the proxy, so they carry no port."""
    query = urlencode({"server": hostname, "secret": secret})
    return ProxyLinks(f"tg://webproxy?{query}", f"https://t.me/webproxy?{query}")


def parse_meta(text: str) -> dict[str, str]:
    """`key=value` lines of /data/mtproxy-meta and /data/tproxy-meta."""
    meta = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep:
            meta[key] = value.strip()
    return meta


@dataclass(frozen=True)
class TelemtSwitches:
    """The TOML booleans of Telemt the app does not show; the defaults of genTelemtVars."""

    use_middle_proxy: str = "true"
    mask: str = "true"
    tls_emulation: str = "false"

    def variables(self) -> dict[str, str]:
        return {
            "$TELEMT_USE_MIDDLE_PROXY": self.use_middle_proxy,
            "$TELEMT_MASK": self.mask,
            "$TELEMT_TLS_EMULATION": self.tls_emulation,
        }


@dataclass(frozen=True, repr=False)
class TelemtConfig:
    """What TelemtInstaller::extractConfigFromContainer reads from /data/config.toml.

    `switches` keeps `use_middle_proxy`, `mask` and `tls_emulation` as written, so the config
    can be written again as it was.
    """

    user: str = ""
    secret: str = ""
    mode: str = STANDARD
    tls_domain: str = ""
    public_host: str = ""
    port: int | None = None
    tag: str = ""
    additional_secrets: tuple[str, ...] = ()
    nat_ip: str = ""
    switches: TelemtSwitches = TelemtSwitches()

    @classmethod
    def parse(cls, text: str) -> "TelemtConfig":
        values: dict[str, str] = {}
        user = secret = ""
        extras: list[str] = []
        section = ""
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("["):
                section = line
                continue
            key, sep, value = line.partition("=")
            if not sep:
                continue
            key, value = key.strip(), _toml_value(value)
            if section == "[access.users]":
                if key.startswith("extra"):
                    extras.append(value)
                elif not user:
                    user, secret = key, proxy_secret(value)
                continue
            values.setdefault(f"{section}{key}", value)
        port = values.get("[server]port", "")
        return cls(
            user=user,
            secret=secret,
            mode=FAKETLS if values.get("[general.modes]tls") == "true" else STANDARD,
            tls_domain=values.get("[censorship]tls_domain", ""),
            public_host=values.get("[general.links]public_host", ""),
            port=int(port) if port.isdigit() else None,
            tag=values.get("[general]ad_tag", ""),
            additional_secrets=tuple(extras),
            nat_ip=values.get("[general]middle_proxy_nat_ip", ""),
            switches=TelemtSwitches(
                use_middle_proxy=values.get("[general]use_middle_proxy", "true"),
                mask=values.get("[censorship]mask", "true"),
                tls_emulation=values.get("[censorship]tls_emulation", "false"),
            ),
        )


def _toml_value(raw: str) -> str:
    """A TOML value as the app reads it: a quoted string or a bare value before ` #`."""
    value = raw.strip()
    if value.startswith('"'):
        last = value.rfind('"')
        return value[1:last] if last > 0 else value[1:]
    return value.partition(" #")[0].strip()


def proxy_links(
    container: str, *, secret: str, port: int | None, server_host: str, settings: dict[str, str]
) -> ProxyLinks | None:
    """The links of a Telegram proxy with the `settings` the import read; None if it has none.

    MTProxy and Telemt use the public host, or the server address when it is empty, as the app.
    """
    if container not in TELEGRAM_PROXIES or not secret:
        return None
    if container == TPROXY_CONTAINER:
        hostname = settings.get("hostname", "")
        return web_proxy_links(hostname, secret) if hostname else None
    mode, tls_domain = settings.get("mode", STANDARD), settings.get("tls_domain", "")
    link_secret = client_secret(secret, mode, tls_domain)
    host = settings.get("public_host") or server_host
    return mtproto_links(host, port or MTPROXY_PORT, link_secret)
