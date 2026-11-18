"""XRay keys as Clash/Mihomo (YAML) and sing-box (JSON) client profiles for a subscription.

The same VLESS Reality over raw TCP the app's `vless://` link carries, written in the formats of
clients that take a whole profile rather than a list of links. Every profile has one selector
named after the panel with all keys in it; everything not private goes through it.
"""

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from typing import Any

from amnezia_core.xray import DEFAULT_FLOW, XrayClientProfile

# Names the clients reserve for their own outbounds.
_RESERVED_NAMES = frozenset({"DIRECT", "REJECT", "direct"})
_DIRECT = "direct"
_CLASH_PORT = 7890
_SINGBOX_TUN = ["172.19.0.1/30", "fdfe:dcba:9876::1/126"]


@dataclass(frozen=True, repr=False)
class XrayProxyList:
    """Named XRay client profiles under one selector. Names become unique: clients demand it.

    `title` names the selector; a comma would break a Clash rule, so it turns into a space.
    """

    title: str
    proxies: tuple[tuple[str, XrayClientProfile], ...]

    @classmethod
    def of(cls, title: str, proxies: Sequence[tuple[str, XrayClientProfile]]) -> "XrayProxyList":
        group = " ".join(title.replace(",", " ").split()) or "Proxy"
        taken = set(_RESERVED_NAMES) | {group}
        named = []
        for name, profile in proxies:
            unique = _unique(name.strip() or "XRay", taken)
            taken.add(unique)
            named.append((unique, profile))
        return cls(group, tuple(named))

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.proxies]

    def clash(self) -> str:
        """A Mihomo (Clash.Meta) profile: the proxies, a select group and the final rule."""
        config = {
            "mixed-port": _CLASH_PORT,
            "mode": "rule",
            "log-level": "warning",
            "proxies": [_clash_proxy(name, profile) for name, profile in self.proxies],
            "proxy-groups": [
                {"name": self.title, "type": "select", "proxies": self.names or ["REJECT"]}
            ],
            "rules": [f"MATCH,{self.title}"],
        }
        return "".join(f"{line}\n" for line in _yaml(config))

    def singbox(self) -> str:
        """A sing-box 1.11+ profile: TUN in, a selector over the VLESS outbounds, direct for LAN."""
        outbounds: list[dict[str, Any]] = [
            _singbox_outbound(name, profile) for name, profile in self.proxies
        ]
        rules: list[dict[str, Any]] = [
            {"action": "sniff"},
            {"protocol": "dns", "action": "hijack-dns"},
            {"ip_is_private": True, "outbound": _DIRECT},
        ]
        route: dict[str, Any] = {"rules": rules, "auto_detect_interface": True}
        if outbounds:
            selector = {"type": "selector", "tag": self.title, "outbounds": self.names}
            outbounds.insert(0, selector)
            route["final"] = self.title
        else:
            rules.append({"action": "reject"})
        config = {
            "log": {"level": "warn"},
            "inbounds": [
                {
                    "type": "tun",
                    "tag": "tun-in",
                    "address": _SINGBOX_TUN,
                    "auto_route": True,
                    "strict_route": True,
                }
            ],
            "outbounds": [*outbounds, {"type": _DIRECT, "tag": _DIRECT}],
            "route": route,
        }
        return json.dumps(config, ensure_ascii=False, indent=2) + "\n"


def _unique(name: str, taken: set[str]) -> str:
    candidate, number = name, 1
    while candidate in taken:
        number += 1
        candidate = f"{name} ({number})"
    return candidate


def _flow(profile: XrayClientProfile) -> str:
    """The vision flow without `-udp443`: neither Mihomo nor sing-box knows the suffix."""
    return replace(profile.server, flow=profile.server.flow or DEFAULT_FLOW).server_flow


def _clash_proxy(name: str, profile: XrayClientProfile) -> dict[str, Any]:
    return {
        "name": name,
        "type": "vless",
        "server": profile.host,
        "port": profile.server.port,
        "uuid": profile.client_id,
        "network": "tcp",
        "udp": True,
        "tls": True,
        "flow": _flow(profile),
        "servername": profile.server.sni,
        "client-fingerprint": profile.server.fingerprint,
        "reality-opts": {"public-key": profile.public_key, "short-id": profile.short_id},
    }


def _singbox_outbound(name: str, profile: XrayClientProfile) -> dict[str, Any]:
    return {
        "type": "vless",
        "tag": name,
        "server": profile.host,
        "server_port": profile.server.port,
        "uuid": profile.client_id,
        "flow": _flow(profile),
        "tls": {
            "enabled": True,
            "server_name": profile.server.sni,
            "utls": {"enabled": True, "fingerprint": profile.server.fingerprint},
            "reality": {
                "enabled": True,
                "public_key": profile.public_key,
                "short_id": profile.short_id,
            },
        },
    }


def _yaml(value: dict[str, Any], indent: str = "") -> Iterator[str]:
    """Block YAML of a mapping; scalars are JSON, which YAML reads as they are."""
    for key, item in value.items():
        if isinstance(item, dict):
            yield f"{indent}{key}:"
            yield from _yaml(item, indent + "  ")
        elif isinstance(item, list) and item and all(isinstance(x, dict) for x in item):
            yield f"{indent}{key}:"
            for entry in item:
                first, *rest = _yaml(entry, indent + "  ")
                yield f"{indent}- {first.lstrip()}"
                yield from rest
        else:
            yield f"{indent}{key}: {json.dumps(item, ensure_ascii=False)}"
