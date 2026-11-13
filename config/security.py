"""Security middleware: the Content-Security-Policy header and headers only a trusted proxy sets."""

import ipaddress
from collections.abc import Callable, Iterable

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest, HttpResponseBase

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


class ContentSecurityPolicyMiddleware:
    """Sends settings.CONTENT_SECURITY_POLICY with every response that has none of its own."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        response = self.get_response(request)
        response.headers.setdefault("Content-Security-Policy", settings.CONTENT_SECURITY_POLICY)
        return response


def policy_allowing(origins: Iterable[str]) -> str:
    """settings.CONTENT_SECURITY_POLICY that also lets the page's scripts request `origins`.

    A view sets it on its own response, and the middleware leaves that header as it is.
    """
    sources = " ".join(("'self'", *sorted(set(origins))))
    return f"{settings.CONTENT_SECURITY_POLICY}; connect-src {sources}"


class TrustedProxyMiddleware:
    """Drops X-Forwarded-For and X-Forwarded-Proto unless they come from settings.TRUSTED_PROXIES.

    nginx or Caddy in front of the panel sets them; anyone who reaches the web port directly
    could set them too and pass for another address or for HTTPS.
    """

    HEADERS = ("HTTP_X_FORWARDED_FOR", "HTTP_X_FORWARDED_PROTO")

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response
        self.proxies = self._networks(settings.TRUSTED_PROXIES)

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        if not self._trusted(str(request.META.get("REMOTE_ADDR", ""))):
            for header in self.HEADERS:
                request.META.pop(header, None)
        return self.get_response(request)

    def _trusted(self, address: str) -> bool:
        try:
            peer = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(peer in network for network in self.proxies)

    @staticmethod
    def _networks(values: list[str]) -> list[Network]:
        try:
            return [ipaddress.ip_network(value.strip(), strict=False) for value in values]
        except ValueError as error:
            raise ImproperlyConfigured(f"LAMNEZIA_TRUSTED_PROXIES: {error}") from error
