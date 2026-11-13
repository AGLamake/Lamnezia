from ipaddress import IPv4Interface

from amnezia_core.awg import AwgServerConfig, Peer
from amnezia_core.awg_backup import AwgServerFiles, interface_config
from amnezia_core.awg_export import AwgServerSettings
from amnezia_core.keys import KeyPair

KEYS = KeyPair.generate()
PARAMS = {"Jc": "5", "Jmin": "10", "S1": "12", "H1": "1", "I1": "<r 2>", "DisableCookies": "on"}


def test_interface_config_is_laid_out_as_configure_container_writes_it() -> None:
    config = interface_config(KEYS.private_key, IPv4Interface("10.8.1.0/24"), 55424, PARAMS)

    assert config.serialize() == (
        "[Interface]\n"
        f"PrivateKey = {KEYS.private_key}\n"
        "Address = 10.8.1.0/24\n"
        "ListenPort = 55424\n"
        "Jc = 5\nJmin = 10\nS1 = 12\nH1 = 1\nDisableCookies = on\n"
        "# I1 = <r 2>\n"
    )


def test_interface_config_reads_back_as_the_same_settings() -> None:
    config = interface_config(KEYS.private_key, IPv4Interface("10.9.0.0/24"), 40000, PARAMS)

    settings = AwgServerSettings.from_config(config)

    assert (settings.port, settings.subnet, dict(settings.params)) == (
        40000,
        IPv4Interface("10.9.0.0/24"),
        PARAMS,
    )
    assert config.private_key == KEYS.private_key
    assert config.peers == []


def test_peers_can_be_added_to_the_interface_config() -> None:
    config = interface_config(KEYS.private_key, IPv4Interface("10.8.1.0/24"), 55424, {})

    config.add_peer(Peer("client=", ("10.8.1.2/32",), "psk="))

    assert AwgServerConfig.parse(config.serialize()).peers == [
        Peer("client=", ("10.8.1.2/32",), "psk=")
    ]


def test_server_files_hold_the_keys_and_end_with_the_config() -> None:
    config = interface_config(KEYS.private_key, IPv4Interface("10.8.1.0/24"), 55424, {})
    files = AwgServerFiles(config.serialize(), "[]", "psk=")

    by_path = files.by_path()

    assert list(by_path)[-1] == "/opt/amnezia/awg/awg0.conf"
    assert by_path["/opt/amnezia/awg/wireguard_server_private_key.key"] == (
        f"{KEYS.private_key}\n".encode()
    )
    assert by_path["/opt/amnezia/awg/wireguard_server_public_key.key"] == (
        f"{KEYS.public_key}\n".encode()
    )
    assert by_path["/opt/amnezia/awg/wireguard_psk.key"] == b"psk=\n"
    assert by_path["/opt/amnezia/awg/clientsTable"] == b"[]"
