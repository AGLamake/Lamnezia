import json

from amnezia_core.keys import KeyPair
from amnezia_core.vpn_link import DEFAULT_DNS
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.profile import KeyExport, ServerProfile
from apps.keys.tests.conftest import HOST, PSK

PROFILE = ServerProfile(
    "amnezia-awg2", HOST, 55424, "10.8.1.0/24", {"Jc": "4"}, KeyPair.generate().public_key, PSK
)


def key_with(profile: str) -> AccessKey:
    pair = KeyPair.generate()
    return AccessKey(
        name="iPhone",
        public_key=pair.public_key,
        private_key=pair.private_key,
        profile=profile,
        client_ip="10.8.1.5",
        status=KeyStatus.ACTIVE,
    )


def test_export_uses_the_dns_of_the_profile() -> None:
    profile = PROFILE.updated(40000, {"Jc": "8"}, ("9.9.9.9", "149.112.112.112"))

    conf = KeyExport.of(key_with(profile.to_json())).files[0].content

    assert "DNS = 9.9.9.9, 149.112.112.112" in conf
    assert "ListenPort" not in conf
    assert f"Endpoint = {HOST}:40000" in conf
    assert "Jc = 8" in conf


def test_a_profile_saved_without_dns_gets_the_default() -> None:
    data = json.loads(PROFILE.to_json())
    del data["dns"]

    assert ServerProfile.from_json(json.dumps(data)).dns == DEFAULT_DNS
    assert ServerProfile.from_json(PROFILE.to_json()) == PROFILE
