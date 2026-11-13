import pytest

from amnezia_core.errors import ConfigFormatError
from amnezia_core.proxies import (
    MTPROXY_CONTAINER,
    SOCKS5_CONTAINER,
    TELEMT_CONTAINER,
    TPROXY_CONTAINER,
    Socks5Account,
    TelemtConfig,
    client_secret,
    configure_secret,
    parse_meta,
    proxy_links,
    proxy_secret,
)

SECRET = "0123456789abcdef0123456789abcdef"


def test_socks5_account_is_read_from_3proxy_cfg() -> None:
    config = "#!/bin/3proxy\nusers proxy_user:CL:Abc123\nauth strong\n"

    account = Socks5Account.from_config(config)

    assert account == Socks5Account("proxy_user", "Abc123")
    assert account.users_line == "users proxy_user:CL:Abc123"
    assert "Abc123" not in repr(account)
    assert Socks5Account.from_config("auth none\n") is None


def test_generated_socks5_password_is_like_the_app() -> None:
    account = Socks5Account.generate()

    assert account.user == "proxy_user"
    assert len(account.password) == 16 and account.password.isalnum()


def test_secret_must_be_32_hex_digits() -> None:
    assert proxy_secret(f" {SECRET}\n") == SECRET
    assert proxy_secret("not a secret") == ""
    assert configure_secret(f"[*] Port: 443\n[*] Secret:    {SECRET}\n") == SECRET
    with pytest.raises(ConfigFormatError):
        configure_secret("[!] ERROR")


def test_link_secret_has_the_prefix_of_the_mode() -> None:
    assert client_secret(SECRET, "standard", "") == f"dd{SECRET}"
    assert client_secret(SECRET, "faketls", "a.io") == f"ee{SECRET}{b'a.io'.hex()}"
    assert client_secret(SECRET, "faketls", "").endswith(b"googletagmanager.com".hex())


def test_mtproto_links_use_the_public_host_or_the_server() -> None:
    links = proxy_links(
        MTPROXY_CONTAINER, secret=SECRET, port=993, server_host="1.2.3.4", settings={}
    )
    assert links
    assert links.tg == f"tg://proxy?server=1.2.3.4&port=993&secret=dd{SECRET}"
    assert links.tme == f"https://t.me/proxy?server=1.2.3.4&port=993&secret=dd{SECRET}"

    public = proxy_links(
        TELEMT_CONTAINER,
        secret=SECRET,
        port=443,
        server_host="1.2.3.4",
        settings={"public_host": "tg.example.com"},
    )
    assert public
    assert "server=tg.example.com&" in public.tg


def test_web_proxy_link_has_the_domain_and_no_port() -> None:
    links = proxy_links(
        TPROXY_CONTAINER,
        secret=SECRET,
        port=443,
        server_host="1.2.3.4",
        settings={"hostname": "w.example.com"},
    )

    assert links
    assert links.tg == f"tg://webproxy?server=w.example.com&secret={SECRET}"
    assert links.tme == f"https://t.me/webproxy?server=w.example.com&secret={SECRET}"


def test_no_links_without_a_secret_or_for_socks5() -> None:
    common = {"port": 443, "server_host": "1.2.3.4", "settings": {}}

    assert proxy_links(MTPROXY_CONTAINER, secret="", **common) is None  # type: ignore[arg-type]
    assert proxy_links(SOCKS5_CONTAINER, secret=SECRET, **common) is None  # type: ignore[arg-type]
    assert proxy_links(TPROXY_CONTAINER, secret=SECRET, **common) is None  # type: ignore[arg-type]


def test_meta_lines_are_key_value() -> None:
    assert parse_meta("mode=faketls\ndomain=a.io\nbroken\npublic_host=\n") == {
        "mode": "faketls",
        "domain": "a.io",
        "public_host": "",
    }


def test_telemt_config_is_read_as_the_app_reads_it() -> None:
    config = TelemtConfig.parse(
        "[general.modes]\ntls = true  # faketls\n\n"
        '[general.links]\npublic_host = "tg.example.com"\n\n'
        "[server]\nport = 8443\n\n"
        '[censorship]\ntls_domain = "a.io"\n\n'
        f'[access.users]\namnezia = "{SECRET}"\nextra_1 = "{"f" * 32}"\n'
    )

    assert (config.user, config.secret, config.port) == ("amnezia", SECRET, 8443)
    assert (config.mode, config.tls_domain, config.public_host) == (
        "faketls",
        "a.io",
        "tg.example.com",
    )
    assert TelemtConfig.parse("").mode == "standard"
