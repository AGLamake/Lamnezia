"""Settings of MTProxy and Telemt the Amnezia app lets one choose, and their script variables.

Follows MtProxyProtocolConfig and TelemtProtocolConfig with genMtProxyVars and genTelemtVars of
upstream scriptsRegistry.cpp: the transport mode (standard `dd` or FakeTLS `ee`), the FakeTLS
domain, the public host of the links, the promotion tag, additional secrets and NAT. The app
reads them back from /data/mtproxy-meta and /data/config.toml, which configure_container.sh writes.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Self

from amnezia_core.proxies import DEFAULT_TLS_DOMAIN, FAKETLS, STANDARD, TelemtConfig

# A promotion tag of @MTProxybot and a secret are both 32 hex digits.
_HEX32 = re.compile(r"[0-9a-fA-F]{32}")
# genMtProxyVars: the automatic mode runs no workers with FakeTLS and 2 in the standard mode.
_AUTO_WORKERS = {FAKETLS: "0", STANDARD: "2"}


def is_hex32(value: str) -> bool:
    """A secret or a promotion tag: 32 hex digits."""
    return bool(_HEX32.fullmatch(value))


def parse_secret_list(text: str) -> tuple[str, ...]:
    """Secrets separated by commas or spaces, as the app keeps them in one line."""
    return tuple(part for part in re.split(r"[\s,]+", text) if part)


@dataclass(frozen=True)
class TelegramProxyOptions:
    """What the app lets one set for MTProxy and Telemt; the defaults are those of the app.

    NAT is on when an external address is given: MTProxy needs the internal one too, Telemt
    only the external one (`middle_proxy_nat_ip`). `workers` is a manual worker count of
    MTProxy; empty means the automatic mode of the app.
    """

    mode: str = STANDARD
    tls_domain: str = DEFAULT_TLS_DOMAIN
    public_host: str = ""
    tag: str = ""
    additional_secrets: tuple[str, ...] = ()
    nat_internal_ip: str = ""
    nat_external_ip: str = ""
    workers: str = ""

    @classmethod
    def from_params(cls, params: Mapping[str, Any]) -> Self:
        return cls(
            mode=str(params.get("mode") or STANDARD),
            tls_domain=str(params.get("tls_domain") or DEFAULT_TLS_DOMAIN),
            public_host=str(params.get("public_host") or ""),
            tag=str(params.get("tag") or ""),
            additional_secrets=tuple(params.get("additional_secrets") or ()),
            nat_internal_ip=str(params.get("nat_internal_ip") or ""),
            nat_external_ip=str(params.get("nat_external_ip") or ""),
            workers=str(params.get("workers") or ""),
        )

    @classmethod
    def from_mtproxy_meta(cls, meta: Mapping[str, str]) -> Self:
        """The settings an MTProxy keeps in /data/mtproxy-meta, as the app restores them."""
        nat = meta.get("nat_enabled") == "1"
        return cls.from_params(
            {
                "mode": meta.get("mode"),
                "tls_domain": meta.get("domain"),
                "public_host": meta.get("public_host"),
                "tag": meta.get("tag"),
                "additional_secrets": parse_secret_list(meta.get("additional", "")),
                "nat_internal_ip": meta.get("nat_internal") if nat else "",
                "nat_external_ip": meta.get("nat_external") if nat else "",
                "workers": meta.get("workers") if meta.get("workers_mode") == "manual" else "",
            }
        )

    @classmethod
    def from_telemt_config(cls, config: TelemtConfig) -> Self:
        """The settings a Telemt keeps in /data/config.toml."""
        return cls.from_params(
            {
                "mode": config.mode,
                "tls_domain": config.tls_domain,
                "public_host": config.public_host,
                "tag": config.tag,
                "additional_secrets": config.additional_secrets,
                "nat_external_ip": config.nat_ip,
            }
        )

    def to_params(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "tls_domain": self.tls_domain,
            "public_host": self.public_host,
            "tag": self.tag,
            "additional_secrets": list(self.additional_secrets),
            "nat_internal_ip": self.nat_internal_ip,
            "nat_external_ip": self.nat_external_ip,
            "workers": self.workers,
        }

    @property
    def faketls(self) -> bool:
        return self.mode == FAKETLS

    def mtproxy_variables(self) -> dict[str, str]:
        """genMtProxyVars without the port and the secret."""
        nat = bool(self.nat_internal_ip and self.nat_external_ip)
        return {
            "$MTPROXY_TAG": self.tag,
            "$MTPROXY_TRANSPORT_MODE": self.mode,
            "$MTPROXY_TLS_DOMAIN": self.tls_domain,
            "$MTPROXY_PUBLIC_HOST": self.public_host,
            "$MTPROXY_ADDITIONAL_SECRETS": ",".join(self.additional_secrets),
            "$MTPROXY_WORKERS_MODE": "manual" if self.workers else "auto",
            "$MTPROXY_WORKERS": self.workers or _AUTO_WORKERS[self.mode],
            "$MTPROXY_NAT_ENABLED": "1" if nat else "0",
            "$MTPROXY_NAT_INTERNAL_IP": self.nat_internal_ip if nat else "",
            "$MTPROXY_NAT_EXTERNAL_IP": self.nat_external_ip if nat else "",
        }

    def telemt_variables(self) -> dict[str, str]:
        """genTelemtVars without the port, the secret, the user and the switches."""
        return {
            "$TELEMT_TOML_SECURE": _toml_bool(not self.faketls),
            "$TELEMT_TOML_TLS": _toml_bool(self.faketls),
            "$TELEMT_TAG": self.tag,
            "$TELEMT_TLS_DOMAIN": self.tls_domain,
            "$TELEMT_PUBLIC_HOST": self.public_host,
            "$TELEMT_ADDITIONAL_SECRETS": ",".join(self.additional_secrets),
            "$TELEMT_MIDDLE_PROXY_NAT_IP": self.nat_external_ip,
        }


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"
