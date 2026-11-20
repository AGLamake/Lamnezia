"""Proxies of the panel's own requests to Telegram, Discord, GitHub and material links.

The panel in a country that blocks api.telegram.org, discord.com or api.github.com reaches them
through a proxy or a self-hosted Bot API server set in the environment. Only these requests
change: the VPN servers (SSH) and webhooks to other sites are reached as before, webhooks with
their address guard. A proxy URL may hold a login and a password: it is shown only as
`OutboundProxy.safe`, never logged.
"""

import http.client
import socket
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Self
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from python_socks import ProxyError
from python_socks.sync import Proxy

PROXY_SCHEMES = ("http", "socks5")
TELEGRAM_API_SERVER = "https://api.telegram.org"
# Discord's API and channel webhooks live on these hosts.
DISCORD_HOSTS = frozenset(
    {"discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com"}
)


@dataclass(frozen=True)
class OutboundProxy:
    """A proxy: `http://` or `socks5://host:port`, with an optional `user:password@`.

    SOCKS5 resolves the names on the proxy side, so a blocked DNS answer does not matter.
    """

    url: str = field(repr=False)

    def __post_init__(self) -> None:
        parts = urlsplit(self.url)
        try:
            port = parts.port
        except ValueError:
            port = None
        if parts.scheme not in PROXY_SCHEMES or not parts.hostname or port is None:
            raise ImproperlyConfigured(
                f"proxy {self.safe}: expected http://host:port or socks5://host:port"
            )

    @classmethod
    def parse(cls, url: str) -> Self | None:
        """None for an empty setting: the requests go directly."""
        url = url.strip()
        return cls(url) if url else None

    @property
    def safe(self) -> str:
        """The URL without the login and the password."""
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}"

    def __str__(self) -> str:
        return self.safe

    def handlers(self) -> list[urllib.request.BaseHandler]:
        """urllib handlers that send every request through this proxy and ignore the
        proxies of the environment.
        """
        if urlsplit(self.url).scheme == "http":
            return [urllib.request.ProxyHandler({"http": self.url, "https": self.url})]
        return [
            urllib.request.ProxyHandler({}),
            _SocksHttpHandler(self.url),
            _SocksHttpsHandler(self.url),
        ]


def url_opener(proxy: OutboundProxy | None) -> Callable[..., Any]:
    """`urlopen`, through the proxy when there is one."""
    if proxy is None:
        return urllib.request.urlopen
    return urllib.request.build_opener(*proxy.handlers()).open


def proxied_opener(
    proxy: OutboundProxy | None, *handlers: urllib.request.BaseHandler
) -> Callable[..., Any]:
    """`urlopen` with the extra handlers, through the proxy when there is one."""
    own = proxy.handlers() if proxy else []
    return urllib.request.build_opener(*own, *handlers).open


@dataclass(frozen=True)
class TelegramApi:
    """Where the panel calls the Bot API: Telegram's server or a self-hosted one, maybe through
    a proxy. The address is the panel's own setting, not a form field, so it is not guarded
    like a webhook address: a self-hosted server may well be in a private network.
    """

    server: str = TELEGRAM_API_SERVER
    proxy: OutboundProxy | None = None

    def __post_init__(self) -> None:
        if urlsplit(self.server).scheme not in ("http", "https"):
            raise ImproperlyConfigured("the Bot API server must be an http or https address")

    @classmethod
    def from_settings(cls) -> Self:
        server = settings.TELEGRAM_API_SERVER.strip().rstrip("/") or TELEGRAM_API_SERVER
        return cls(server, OutboundProxy.parse(settings.TELEGRAM_PROXY))

    def method_url(self, token: str, method: str) -> str:
        return f"{self.server}/bot{token}/{method}"

    def opener(self, *handlers: urllib.request.BaseHandler) -> Callable[..., Any]:
        """`urlopen` with the extra handlers, through the proxy when there is one."""
        return proxied_opener(self.proxy, *handlers)


@dataclass(frozen=True)
class DiscordApi:
    """How the panel reaches Discord's own hosts (the API and channel webhooks): through the
    proxy of the settings. Without a proxy, or for a webhook on another host, nothing changes.
    """

    proxy: OutboundProxy | None = None

    @classmethod
    def from_settings(cls) -> Self:
        return cls(OutboundProxy.parse(settings.DISCORD_PROXY))

    def route(self, url: str) -> Self | None:
        """Self for an address on Discord's hosts when there is a proxy; None otherwise: the
        request goes as before, with the webhook guard.
        """
        if self.proxy is None or urlsplit(url).hostname not in DISCORD_HOSTS:
            return None
        return self

    def opener(self, *handlers: urllib.request.BaseHandler) -> Callable[..., Any]:
        """`urlopen` with the extra handlers, through the proxy."""
        return proxied_opener(self.proxy, *handlers)


# A request to a messenger's own service, set in the environment, not to an address from a form.
MessengerApi = TelegramApi | DiscordApi


def _socks_connect(
    proxy_url: str,
    address: tuple[str, int],
    timeout: object = None,
    source_address: object = None,
) -> socket.socket:
    """`socket.create_connection` through a SOCKS proxy; its refusal (a wrong password, an
    unreachable target) is a connection error, as urllib expects.
    """
    host, port = address
    seconds = timeout if isinstance(timeout, int | float) else None
    try:
        sock: socket.socket = Proxy.from_url(proxy_url).connect(host, port, timeout=seconds)
    except ProxyError as error:
        raise ConnectionError(f"the proxy refused: {error}") from None
    return sock


def _through[T: http.client.HTTPConnection](proxy_url: str, connection: T) -> T:
    connection._create_connection = partial(_socks_connect, proxy_url)  # type: ignore[attr-defined]
    return connection


class _SocksHttpHandler(urllib.request.HTTPHandler):
    def __init__(self, proxy_url: str) -> None:
        super().__init__()
        self._proxy_url = proxy_url

    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(self._connection, req)

    def _connection(self, host: str, **kwargs: Any) -> http.client.HTTPConnection:
        return _through(self._proxy_url, http.client.HTTPConnection(host, **kwargs))


class _SocksHttpsHandler(urllib.request.HTTPSHandler):
    def __init__(self, proxy_url: str) -> None:
        super().__init__()
        self._proxy_url = proxy_url

    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(self._connection, req)

    def _connection(self, host: str, **kwargs: Any) -> http.client.HTTPSConnection:
        kwargs.setdefault("context", self._context)  # type: ignore[attr-defined]
        return _through(self._proxy_url, http.client.HTTPSConnection(host, **kwargs))
