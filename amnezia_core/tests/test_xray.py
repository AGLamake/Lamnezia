import json
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

import pytest

from amnezia_core.clients_table import ClientRecord, ClientsTable
from amnezia_core.errors import ConfigFormatError, ProtectedClientError, UnsupportedSetupError
from amnezia_core.vpn_link import decode_vpn_link, encode_vpn_link
from amnezia_core.xray import (
    XrayClientProfile,
    XrayServerConfig,
    XraySettings,
    client_fingerprint,
    is_valid_site,
)
from amnezia_core.xray_clients import XrayClientRegistry

OWN_CLIENT = "6f0c3e52-1b7a-4c8e-9d2f-0a1b2c3d4e5f"
NEW_CLIENT = "0d4f7a2b-8c3e-4f1a-9b6d-5e2c1a0f9d8e"
PUBLIC_KEY = "Z84J2IelR9ch3k8VtlVhhs5ycBUlXA7wHBWcBrjqnAw"
SHORT_ID = "a1b2c3d4e5f60718"
CREATED = datetime(2026, 11, 3, 10, 0, 0)

# Written by hand from XrayProtocolConfig::toClientOutboundJson for the default install.
EXPECTED_NATIVE = {
    "log": {"loglevel": "error"},
    "inbounds": [
        {"listen": "127.0.0.1", "port": 10808, "protocol": "socks", "settings": {"udp": True}}
    ],
    "outbounds": [
        {
            "protocol": "vless",
            "settings": {
                "vnext": [
                    {
                        "address": "vpn.example.com",
                        "port": 443,
                        "users": [
                            {"id": NEW_CLIENT, "encryption": "none", "flow": "xtls-rprx-vision"}
                        ],
                    }
                ]
            },
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "fingerprint": "chrome",
                    "serverName": "www.googletagmanager.com",
                    "publicKey": PUBLIC_KEY,
                    "shortId": SHORT_ID,
                    "spiderX": "",
                },
            },
        }
    ],
}


@pytest.fixture
def server_text(read_fixture: Callable[[str], str]) -> str:
    return read_fixture("xray_server.json")


@pytest.fixture
def profile(server_text: str) -> XrayClientProfile:
    settings = XrayServerConfig.parse(server_text).settings()
    return XrayClientProfile("vpn.example.com", settings, NEW_CLIENT, PUBLIC_KEY, SHORT_ID)


def test_round_trip_keeps_the_file_written_by_the_app(server_text: str) -> None:
    assert XrayServerConfig.parse(server_text).serialize() == server_text


def test_reads_reality_settings_of_the_default_install(server_text: str) -> None:
    assert XrayServerConfig.parse(server_text).settings() == XraySettings()


def test_reads_custom_port_site_and_udp443_flow() -> None:
    config = XrayServerConfig(
        {
            "inbounds": [
                {
                    "port": 8443,
                    "settings": {"clients": [{"id": OWN_CLIENT, "flow": "xtls-rprx-vision"}]},
                    "streamSettings": {
                        "security": "reality",
                        "realitySettings": {"dest": "example.org:443", "serverName": "cdn.example"},
                    },
                }
            ]
        }
    )

    settings = config.settings()

    assert (settings.port, settings.site, settings.sni) == (8443, "example.org", "cdn.example")
    assert XraySettings(flow="xtls-rprx-vision-udp443").server_flow == "xtls-rprx-vision"


def test_site_without_port_and_legacy_fingerprint() -> None:
    reality = {"dest": "example.org", "fingerprint": "Mozilla/5.0 (X11)"}
    config = XrayServerConfig(
        {
            "inbounds": [
                {
                    "settings": {"clients": []},
                    "streamSettings": {"security": "reality", "realitySettings": reality},
                }
            ]
        }
    )

    settings = config.settings()

    assert (settings.site, settings.sni, settings.fingerprint) == (
        "example.org",
        "example.org",
        "chrome",
    )
    assert settings.flow == "xtls-rprx-vision"


@pytest.mark.parametrize(
    "stream",
    [
        {"network": "xhttp", "security": "reality"},
        {"network": "tcp", "security": "tls"},
        {"network": "kcp", "security": "none"},
    ],
)
def test_other_transports_are_refused(stream: dict[str, str]) -> None:
    config = XrayServerConfig(
        {"inbounds": [{"settings": {"clients": []}, "streamSettings": stream}]}
    )

    with pytest.raises(UnsupportedSetupError):
        config.settings()


@pytest.mark.parametrize(
    "text",
    ["not json", "[]", '{"inbounds": []}', '{"inbounds": [{"settings": {}}]}'],
)
def test_broken_server_json_is_refused(text: str) -> None:
    with pytest.raises(ConfigFormatError):
        XrayServerConfig.parse(text)


def test_stream_settings_are_required() -> None:
    config = XrayServerConfig({"inbounds": [{"settings": {"clients": []}}]})

    with pytest.raises(ConfigFormatError):
        config.settings()


def test_add_and_remove_client_keep_the_rest(server_text: str) -> None:
    config = XrayServerConfig.parse(server_text)

    config.add_client(NEW_CLIENT, "xtls-rprx-vision")

    assert config.client_ids == [OWN_CLIENT, NEW_CLIENT]
    data = json.loads(config.serialize())
    assert data["inbounds"][0]["settings"]["clients"][1] == {
        "id": NEW_CLIENT,
        "flow": "xtls-rprx-vision",
    }
    with pytest.raises(ConfigFormatError):
        config.add_client(NEW_CLIENT, "")

    config.remove_client(NEW_CLIENT)

    assert config.serialize() == server_text


def test_native_config_matches_client_outbound(profile: XrayClientProfile) -> None:
    assert profile.native_config() == EXPECTED_NATIVE


def test_vpn_config_matches_connection_export(profile: XrayClientProfile) -> None:
    config = profile.vpn_config("Amsterdam")
    xray = config["containers"][0]["xray"]
    last_config = xray.pop("last_config")

    assert config == {
        "format_version": 1,
        "description": "Amsterdam",
        "hostName": "vpn.example.com",
        "containers": [{"container": "amnezia-xray", "xray": xray}],
        "defaultContainer": "amnezia-xray",
        "dns1": "1.1.1.1",
        "dns2": "1.0.0.1",
    }
    assert xray == {
        "port": "443",
        "transport_proto": "tcp",
        "site": "www.googletagmanager.com",
        "xray_settings_version": 2,
        "xray_security": "reality",
        "xray_flow": "xtls-rprx-vision",
        "xray_sni": "www.googletagmanager.com",
        "xray_alpn": "h2",
        "xray_fingerprint": "chrome",
        "xray_transport": "raw",
    }
    # Qt compact JSON with sorted keys, the native config itself.
    assert last_config.startswith('{"inbounds":[{"listen":"127.0.0.1","port":10808,')
    assert json.loads(last_config) == EXPECTED_NATIVE
    assert decode_vpn_link(encode_vpn_link(config))["containers"][0]["xray"]["xray_flow"]


def test_vless_link_matches_export(profile: XrayClientProfile) -> None:
    assert profile.vless_link() == (
        f"vless://{NEW_CLIENT}@vpn.example.com:443?encryption=none&security=reality"
        f"&flow=xtls-rprx-vision&sni=www.googletagmanager.com&fp=chrome&pbk={PUBLIC_KEY}"
        f"&sid={SHORT_ID}#AmneziaVPN"
    )
    assert profile.vless_link("Дом 1").endswith("#%D0%94%D0%BE%D0%BC%201")


def test_vless_link_puts_an_ipv6_host_in_brackets(profile: XrayClientProfile) -> None:
    ipv6 = replace(profile, host="2001:db8::1")

    assert "@[2001:db8::1]:443?" in ipv6.vless_link()
    assert ipv6.native_config()["outbounds"][0]["settings"]["vnext"][0]["address"] == "2001:db8::1"


def test_repr_hides_the_client_id(profile: XrayClientProfile) -> None:
    assert NEW_CLIENT not in repr(profile)


@pytest.fixture
def registry(server_text: str) -> XrayClientRegistry:
    return XrayClientRegistry.parse(server_text, "", OWN_CLIENT)


def test_registry_adds_client_with_server_flow(registry: XrayClientRegistry) -> None:
    registry.add_client(NEW_CLIENT, ClientRecord("Phone", CREATED, 7))

    assert registry.config.client_ids == [OWN_CLIENT, NEW_CLIENT]
    assert registry.clients.user_data(NEW_CLIENT)["lamneziaUserId"] == 7
    assert ClientsTable.parse(registry.clients.serialize()).name(NEW_CLIENT) == "Phone"


def test_registry_changes_are_idempotent(registry: XrayClientRegistry) -> None:
    record = ClientRecord("Phone", CREATED)
    registry.add_client(NEW_CLIENT, record)
    before = registry.config.serialize(), registry.clients.serialize()

    registry.add_client(NEW_CLIENT, record)

    assert (registry.config.serialize(), registry.clients.serialize()) == before


def test_disable_keeps_the_record_and_enable_brings_the_client_back(
    registry: XrayClientRegistry,
) -> None:
    record = ClientRecord("Phone", CREATED)
    registry.add_client(NEW_CLIENT, record)

    registry.disable(NEW_CLIENT)

    assert registry.config.client_ids == [OWN_CLIENT]
    assert registry.clients.name(NEW_CLIENT) == "Phone"

    registry.add_client(NEW_CLIENT, record)

    assert registry.config.client_ids == [OWN_CLIENT, NEW_CLIENT]


def test_revoke_removes_client_and_record(registry: XrayClientRegistry) -> None:
    registry.add_client(NEW_CLIENT, ClientRecord("Phone", CREATED))

    registry.revoke(NEW_CLIENT)

    assert registry.config.client_ids == [OWN_CLIENT]
    assert NEW_CLIENT not in registry.clients


@pytest.mark.parametrize("change", ["disable", "revoke"])
def test_own_client_of_the_app_is_protected(registry: XrayClientRegistry, change: str) -> None:
    with pytest.raises(ProtectedClientError):
        getattr(registry, change)(OWN_CLIENT)

    assert registry.config.client_ids == [OWN_CLIENT]


def test_custom_fingerprint_is_kept() -> None:
    stream = {"security": "reality", "realitySettings": {"fingerprint": "firefox"}}
    config = XrayServerConfig(
        {"inbounds": [{"settings": {"clients": []}, "streamSettings": stream}]}
    )

    assert config.settings().fingerprint == "firefox"


STATS = {
    "stats": {},
    "api": {"tag": "api", "listen": "127.0.0.1:10085", "services": ["StatsService"]},
    "policy": {"levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}}},
}
PRIVATE_KEY = "uGQ5Zl0n6xkVYPWk5aM4lJx2bS7cT8dE9fG0hI1jK2M"


def test_setup_config_is_the_file_the_app_writes_without_access_log(server_text: str) -> None:
    config = XrayServerConfig.for_setup(
        XraySettings(), own_client_id=OWN_CLIENT, private_key=PRIVATE_KEY, short_id=SHORT_ID
    )
    expected = json.loads(server_text)
    expected["log"]["access"] = "none"
    expected["inbounds"][0]["settings"]["clients"][0]["email"] = client_fingerprint(OWN_CLIENT)
    expected.update(STATS)

    assert json.loads(config.serialize()) == expected
    assert config.stats_enabled


def test_setup_config_with_custom_port_site_and_sni() -> None:
    settings = XraySettings(port=8443, site="www.example.com", sni="cdn.example.com")

    config = XrayServerConfig.for_setup(
        settings, own_client_id=OWN_CLIENT, private_key=PRIVATE_KEY, short_id=SHORT_ID
    )

    assert config.settings() == settings
    assert config.short_id == SHORT_ID
    reality = json.loads(config.serialize())["inbounds"][0]["streamSettings"]["realitySettings"]
    assert reality["dest"] == "www.example.com:443"
    assert reality["serverNames"] == ["cdn.example.com"]


def test_short_id_and_private_key_of_the_default_install(server_text: str) -> None:
    config = XrayServerConfig.parse(server_text)

    assert (config.short_id, config.private_key) == (SHORT_ID, PRIVATE_KEY)


def test_client_fingerprint_hides_the_uuid() -> None:
    fingerprint = client_fingerprint(NEW_CLIENT)

    assert len(fingerprint) == 64
    assert NEW_CLIENT not in fingerprint
    assert fingerprint == client_fingerprint(NEW_CLIENT) != client_fingerprint(OWN_CLIENT)


@pytest.mark.parametrize(
    ("site", "valid"),
    [
        ("www.example.com", True),
        ("203.0.113.10", True),
        ("xn--80ak6aa92e.com", True),
        ("", False),
        ("*", False),
        ("*.example.com", False),
        ("example", False),
        ("12345", False),
        ("bad host.com", False),
        ("example.com;reboot", False),
        ("2001:db8::1", False),
        ("a" * 250 + ".com", False),
    ],
)
def test_masking_site_is_checked_as_in_the_app(site: str, valid: bool) -> None:
    assert is_valid_site(site) is valid


def test_registry_renames_a_client(registry: XrayClientRegistry) -> None:
    registry.add_client(NEW_CLIENT, ClientRecord("Phone", CREATED))

    registry.rename(NEW_CLIENT, "Laptop")
    registry.rename("unknown", "Nobody")

    assert registry.clients.name(NEW_CLIENT) == "Laptop"
    assert "unknown" not in registry.clients


def test_server_json_of_the_app_has_no_stats(server_text: str) -> None:
    assert not XrayServerConfig.parse(server_text).stats_enabled


def test_enable_stats_adds_blocks_and_emails_and_keeps_the_rest(server_text: str) -> None:
    config = XrayServerConfig.parse(server_text)
    config.add_client(NEW_CLIENT, "xtls-rprx-vision")

    config.enable_stats()

    data = json.loads(config.serialize())
    original = json.loads(server_text)
    assert {key: data[key] for key in STATS} == STATS
    assert data["inbounds"][0]["streamSettings"] == original["inbounds"][0]["streamSettings"]
    assert data["outbounds"] == original["outbounds"]
    emails = [client["email"] for client in data["inbounds"][0]["settings"]["clients"]]
    assert emails == [client_fingerprint(OWN_CLIENT), client_fingerprint(NEW_CLIENT)]
    assert NEW_CLIENT not in json.dumps({key: data[key] for key in STATS})
    assert config.stats_enabled


def test_enable_stats_keeps_other_policy_settings(server_text: str) -> None:
    data = json.loads(server_text)
    data["policy"] = {
        "levels": {"0": {"handshake": 4}, "1": {"connIdle": 300}},
        "system": {"statsInboundUplink": True},
    }
    config = XrayServerConfig(data)

    config.enable_stats()

    policy = json.loads(config.serialize())["policy"]
    assert policy == {
        "levels": {
            "0": {"handshake": 4, "statsUserUplink": True, "statsUserDownlink": True},
            "1": {"connIdle": 300},
        },
        "system": {"statsInboundUplink": True},
    }


def test_enable_stats_is_idempotent(server_text: str) -> None:
    config = XrayServerConfig.parse(server_text)
    config.enable_stats()
    once = config.serialize()

    config.enable_stats()

    assert config.serialize() == once


def test_rebuild_by_the_app_drops_the_blocks_and_keeps_emails(server_text: str) -> None:
    config = XrayServerConfig.parse(server_text)
    config.enable_stats()
    data = json.loads(config.serialize())
    for name in STATS:
        del data[name]

    rebuilt = XrayServerConfig(data)

    assert not rebuilt.stats_enabled
    rebuilt.enable_stats()
    assert rebuilt.stats_enabled


def test_client_added_by_the_app_without_email_turns_stats_off(server_text: str) -> None:
    config = XrayServerConfig.parse(server_text)
    config.enable_stats()

    config.add_client(NEW_CLIENT, "xtls-rprx-vision")

    assert not config.stats_enabled


def test_new_settings_keep_clients_stats_and_routes(server_text: str) -> None:
    config = XrayServerConfig.parse(server_text)
    config.add_client(NEW_CLIENT, "xtls-rprx-vision")
    config.enable_stats()
    config.set_speed_marks({client_fingerprint(NEW_CLIENT): 101})
    settings = XraySettings(
        port=8443,
        flow="xtls-rprx-vision-udp443",
        site="www.microsoft.com",
        sni="",
        fingerprint="qq",
    )

    config.apply_settings(settings)

    changed = XrayServerConfig.parse(config.serialize())
    assert changed.settings() == XraySettings(
        port=8443,
        flow="xtls-rprx-vision",
        site="www.microsoft.com",
        sni="www.microsoft.com",
    )
    assert changed.client_ids == config.client_ids
    assert changed.stats_enabled
    data = json.loads(config.serialize())
    assert data["routing"]["rules"][-1]["user"] == [client_fingerprint(NEW_CLIENT)]
    reality = data["inbounds"][0]["streamSettings"]["realitySettings"]
    assert reality["dest"] == "www.microsoft.com:443"
    assert "fingerprint" not in reality


def test_empty_flow_is_taken_off_every_client(server_text: str) -> None:
    config = XrayServerConfig.parse(server_text)
    config.add_client(NEW_CLIENT, "xtls-rprx-vision")

    config.apply_settings(XraySettings(flow=""))

    clients = json.loads(config.serialize())["inbounds"][0]["settings"]["clients"]
    assert all("flow" not in client for client in clients)
