import random
from ipaddress import IPv4Network

import pytest

from amnezia_core.awg_export import has_awg3_params
from amnezia_core.awg_install import AwgInstallSettings, generate_awg_params, upgraded_awg_params
from amnezia_core.awg_params import check_awg_params
from amnezia_core.errors import AmneziaFormatError
from amnezia_core.keys import KeyPair
from amnezia_core.script_vars import substitute


def test_params_match_the_app_defaults() -> None:
    params = generate_awg_params(random.Random(1))

    assert 4 <= int(params["Jc"]) <= 6
    assert (params["Jmin"], params["Jmax"]) == ("10", "50")
    assert [params[f"S{n}"] for n in range(1, 5)] == ["12"] * 4
    assert [params[f"H{n}"] for n in range(1, 5)] == ["1", "2", "3", "4"]
    assert params["I1"].startswith("<r 2><b 0x8580000100010000")
    assert (params["RandomTrailers"], params["DisableCookies"]) == ("on", "on")
    assert "ContentPaddingAddition" not in params
    KeyPair.from_private_key(params["HeaderProtectionKey"])


def test_junk_packet_count_covers_the_whole_range() -> None:
    rng = random.Random(7)

    counts = {generate_awg_params(rng)["Jc"] for _ in range(200)}

    assert counts == {"4", "5", "6"}


def test_every_server_gets_its_own_header_protection_key() -> None:
    first, second = AwgInstallSettings(), AwgInstallSettings()

    assert first.params["HeaderProtectionKey"] != second.params["HeaderProtectionKey"]


def test_script_variables_for_custom_port_and_subnet() -> None:
    settings = AwgInstallSettings(port=40001, subnet=IPv4Network("10.9.0.0/22"))

    variables = settings.script_variables()

    assert variables["$AWG_SERVER_PORT"] == "40001"
    assert (variables["$AWG_SUBNET_IP"], variables["$WIREGUARD_SUBNET_CIDR"]) == ("10.9.0.0", "22")
    assert variables["$CONTAINER_NAME"] == "amnezia-awg2"
    assert variables["$DOCKERFILE_FOLDER"] == "/opt/amnezia/amnezia-awg2"
    assert variables["$CONTENT_PADDING_ADDITION"] == ""


def test_switched_off_toggle_is_left_out_like_upstream() -> None:
    settings = AwgInstallSettings(params={"RandomTrailers": "off", "DisableCookies": "on"})

    variables = settings.script_variables()

    assert (variables["$RANDOM_TRAILERS"], variables["$DISABLE_COOKIES"]) == ("", "on")


def test_shell_variables_survive_substitution() -> None:
    text = substitute("$WIREGUARD_SERVER_PRIVATE_KEY $AWG_SERVER_PORT", {"$AWG_SERVER_PORT": "1"})

    assert text == "$WIREGUARD_SERVER_PRIVATE_KEY 1"


@pytest.mark.parametrize(
    "settings",
    [
        {"port": 0},
        {"port": 65536},
        {"subnet": IPv4Network("10.8.1.0/31")},
    ],
)
def test_invalid_settings_are_refused(settings: dict[str, object]) -> None:
    with pytest.raises(AmneziaFormatError):
        AwgInstallSettings(**settings)  # type: ignore[arg-type]


AWG2_PARAMS = {
    "Jc": "4",
    "Jmin": "10",
    "Jmax": "50",
    "S1": "72",
    "S2": "56",
    "S3": "32",
    "S4": "16",
    "H1": "1020325451-1025713284",
    "H2": "1183243451-1193573213",
    "H3": "1451245281-1471212327",
    "H4": "1785210913-1795201230",
    "I1": "<r 2>",
}


def test_upgrade_adds_the_awg3_params_and_keeps_the_rest() -> None:
    params = upgraded_awg_params(AWG2_PARAMS)

    assert {name: params[name] for name in AWG2_PARAMS} == AWG2_PARAMS
    assert has_awg3_params(params) and not has_awg3_params(AWG2_PARAMS)
    KeyPair.from_private_key(params["HeaderProtectionKey"])
    assert (params["RandomTrailers"], params["DisableCookies"]) == ("on", "on")
    assert params["RekeyAfterTime"] == "100-120"
    assert "ContentPaddingAddition" not in params
    assert check_awg_params(params) == []


def test_upgrade_grows_the_padding_header_protection_needs() -> None:
    params = upgraded_awg_params({**AWG2_PARAMS, "S1": "0", "S2": "68", "S3": "", "S4": "5"})

    # S1 = 12 would make the init message as long as the response with S2 = 68.
    assert [params[f"S{n}"] for n in range(1, 5)] == ["13", "68", "12", "12"]
    assert check_awg_params(params) == []


def test_upgrade_keeps_awg3_values_already_set() -> None:
    params = upgraded_awg_params({**AWG2_PARAMS, "RekeyTimeout": "4-6"})

    assert params["RekeyTimeout"] == "4-6"
