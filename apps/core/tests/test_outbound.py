import json
import urllib.error
import urllib.request
from typing import Any

import pytest
from django.core.exceptions import ImproperlyConfigured

from apps.core.services.outbound import (
    TELEGRAM_API_SERVER,
    OutboundProxy,
    TelegramApi,
    url_opener,
)
from apps.core.tests.proxies import socks_proxy, web_server


def test_an_empty_setting_means_no_proxy() -> None:
    assert OutboundProxy.parse("  ") is None
    assert url_opener(None) is urllib.request.urlopen


@pytest.mark.parametrize(
    "url", ["https://proxy:3128", "socks4://proxy:1080", "socks5://proxy", "proxy:1080"]
)
def test_only_http_and_socks5_with_a_port_are_taken(url: str) -> None:
    with pytest.raises(ImproperlyConfigured):
        OutboundProxy(url)


def test_the_password_is_never_shown() -> None:
    proxy = OutboundProxy("socks5://user:s3cret@proxy.example.com:1080")

    assert proxy.safe == "socks5://proxy.example.com:1080"
    assert "s3cret" not in str(proxy)
    assert "s3cret" not in repr(proxy)
    assert "s3cret" not in repr(TelegramApi(proxy=proxy))


def test_a_wrong_proxy_does_not_show_its_password() -> None:
    with pytest.raises(ImproperlyConfigured) as error:
        OutboundProxy("socks4://user:s3cret@proxy:1080")

    assert "s3cret" not in str(error.value)


def test_requests_go_through_an_http_proxy_with_its_login() -> None:
    with web_server() as (port, seen):
        proxy = OutboundProxy(f"http://user:pw@127.0.0.1:{port}")

        with url_opener(proxy)("http://api.github.test/x", timeout=5) as response:
            answer = json.load(response)

    assert answer == {"ok": True}
    assert seen.paths == ["http://api.github.test/x"]
    assert seen.auth == ["Basic dXNlcjpwdw=="]


def test_requests_go_through_a_socks5_proxy_that_resolves_the_name() -> None:
    with web_server() as (port, served), socks_proxy(password=True) as (proxy_port, seen):
        proxy = OutboundProxy(f"socks5://user:pw@127.0.0.1:{proxy_port}")

        with url_opener(proxy)(f"http://api.telegram.test:{port}/x", timeout=5) as response:
            answer = json.load(response)

    assert answer == {"ok": True}
    assert seen.paths == [f"api.telegram.test:{port}"]
    assert seen.auth == ["user:pw"]
    assert served.paths == ["/x"]


def test_the_bot_api_is_telegram_by_default(settings: Any) -> None:
    settings.TELEGRAM_API_SERVER = ""
    settings.TELEGRAM_PROXY = ""

    api = TelegramApi.from_settings()

    assert api == TelegramApi(TELEGRAM_API_SERVER, None)
    assert api.method_url("1:a", "sendMessage") == "https://api.telegram.org/bot1:a/sendMessage"


def test_the_bot_api_server_and_proxy_come_from_the_settings(settings: Any) -> None:
    settings.TELEGRAM_API_SERVER = "http://bot-api:8081/"
    settings.TELEGRAM_PROXY = "socks5://proxy:1080"

    api = TelegramApi.from_settings()

    assert api.method_url("1:a", "getMe") == "http://bot-api:8081/bot1:a/getMe"
    assert api.proxy == OutboundProxy("socks5://proxy:1080")


def test_the_bot_api_server_is_a_web_address() -> None:
    with pytest.raises(ImproperlyConfigured):
        TelegramApi("bot-api:8081")


def test_https_goes_through_a_socks5_proxy_too() -> None:
    with web_server() as (port, _), socks_proxy() as (proxy_port, seen):
        proxy = OutboundProxy(f"socks5://127.0.0.1:{proxy_port}")

        # The stand-in server speaks plain HTTP, so the TLS handshake fails past the proxy.
        with pytest.raises(OSError):
            url_opener(proxy)(f"https://api.github.test:{port}/x", timeout=5)

    assert seen.paths == [f"api.github.test:{port}"]


def test_a_refused_login_is_a_network_error() -> None:
    with socks_proxy(password=True, accept=False) as (proxy_port, _):
        proxy = OutboundProxy(f"socks5://user:s3cret@127.0.0.1:{proxy_port}")

        with pytest.raises(urllib.error.URLError) as error:
            url_opener(proxy)("http://api.telegram.test/x", timeout=5)

    assert "s3cret" not in str(error.value)
