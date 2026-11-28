import pytest

from amnezia_core.awg_fingerprint import (
    default_params,
    fixed_headers,
    is_default_signature,
    obfuscation_signature,
)
from amnezia_core.awg_install import generate_awg_params
from amnezia_core.errors import ConfigFormatError
from amnezia_core.xray import mux_enabled


def test_a_new_server_keeps_the_defaults_and_fixed_headers() -> None:
    params = generate_awg_params()

    assert default_params(params) == [
        "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4"
    ]  # fmt: skip
    assert fixed_headers(params) == ["H1", "H2", "H3", "H4"]
    assert is_default_signature(obfuscation_signature(params))


def test_own_values_and_ranges_are_not_reported() -> None:
    params = {
        "Jmin": "20", "Jmax": "70", "S1": "31", "S2": "47", "S3": "18", "S4": "25",
        "H1": "100-200", "H2": "300-400", "H3": "500-600", "H4": "4",
    }  # fmt: skip

    assert default_params(params) == ["H4"]
    assert fixed_headers(params) == ["H4"]
    assert not is_default_signature(obfuscation_signature(params))


@pytest.mark.parametrize(
    ("config", "enabled"),
    [
        ('{"inbounds": [{"port": 443}]}', False),
        ('{"outbounds": [{"mux": {"enabled": false}}]}', False),
        ('{"outbounds": [{"mux": {"enabled": true, "concurrency": 8}}]}', True),
        ('{"inbounds": [{"xhttpSettings": {"xmux": {"maxConcurrency": 16}}}]}', True),
    ],
)  # fmt: skip
def test_mux_is_found_anywhere_in_server_json(config: str, enabled: bool) -> None:
    assert mux_enabled(config) is enabled


def test_a_broken_server_json_is_refused() -> None:
    with pytest.raises(ConfigFormatError):
        mux_enabled("{")
