import json
from collections.abc import Callable
from dataclasses import replace

import pytest

from amnezia_core.xray import XrayClientProfile, XraySettings
from amnezia_core.xray_formats import XrayProxyList

PUBLIC_KEY = "Z84J2IelR9ch3k8VtlVhhs5ycBUlXA7wHBWcBrjqnAw"
HOME = XrayClientProfile(
    "vpn.example.com",
    XraySettings(443, "xtls-rprx-vision", "www.googletagmanager.com", "www.googletagmanager.com"),
    "0d4f7a2b-8c3e-4f1a-9b6d-5e2c1a0f9d8e",
    PUBLIC_KEY,
    "a1b2c3d4e5f60718",
)
# A second key with the same name, an IPv6 server and the app's `-udp443` flow.
OFFICE = replace(
    HOME,
    host="2001:db8::1",
    server=XraySettings(8443, "xtls-rprx-vision-udp443", "example.org", "example.org", "firefox"),
    client_id="5b1e0c9a-7d2f-4e3b-8a6c-1f0e9d8c7b6a",
)


@pytest.fixture
def proxies() -> XrayProxyList:
    return XrayProxyList.of("Lamnezia, VPN", [("Дом", HOME), ("Дом", OFFICE)])


def test_clash_profile_matches_the_fixture(
    proxies: XrayProxyList, read_fixture: Callable[[str], str]
) -> None:
    assert proxies.clash() == read_fixture("xray_clash.yaml")


def test_singbox_profile_matches_the_fixture(
    proxies: XrayProxyList, read_fixture: Callable[[str], str]
) -> None:
    assert json.loads(proxies.singbox()) == json.loads(read_fixture("xray_singbox.json"))


def test_names_are_unique_and_do_not_take_the_reserved_ones() -> None:
    proxies = XrayProxyList.of("VPN", [("VPN", HOME), ("DIRECT", HOME), ("", HOME), ("", HOME)])

    assert proxies.names == ["VPN (2)", "DIRECT (2)", "XRay", "XRay (2)"]


def test_without_keys_clash_rejects_and_singbox_blocks() -> None:
    proxies = XrayProxyList.of("VPN", [])

    assert '  proxies: ["REJECT"]' in proxies.clash()
    route = json.loads(proxies.singbox())["route"]
    assert "final" not in route
    assert route["rules"][-1] == {"action": "reject"}
