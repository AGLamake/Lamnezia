"""The amnezia-awg2 server of the Amnezia app the AmneziaWG tests start from, and their helpers."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from amnezia_core.awg import AWG_CONFIG_PATH, AwgServerConfig
from amnezia_core.keys import KeyPair
from apps.accounts.models import User
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.profile import ServerProfile
from apps.operations.models import Operation
from apps.protocols.models import ProtocolInstance
from apps.protocols.tests.fake_awg import FakeAwgServer

FIXTURES = Path(__file__).resolve().parents[3] / "amnezia_core" / "tests" / "fixtures"
PSK = "u+xLktHw90auwUVtjZEJVpNCbfbFK8GNmGegY/Z0XYs="
START = b"#!/bin/bash\nawg-quick up /opt/amnezia/awg/awg0.conf\n"
OnCommit = Callable[..., AbstractContextManager[Any]]
Start = Callable[[Callable[[], Operation]], Operation]


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


APP_CONFIG = AwgServerConfig.parse(fixture("awg0.conf").decode())


def issued_key(instance: ProtocolInstance, owner: User | None = None) -> AccessKey:
    pair = KeyPair.generate()
    profile = ServerProfile(
        "amnezia-awg2",
        "203.0.113.10",
        55424,
        "10.8.1.0/24",
        dict(instance.params),
        instance.public_key,
        PSK,
    )
    return AccessKey.objects.create(
        user=owner,
        source=instance.source,
        name="iPhone",
        public_key=pair.public_key,
        private_key=pair.private_key,
        profile=profile.to_json(),
        client_ip="10.8.1.5",
        status=KeyStatus.ACTIVE,
    )


def server_config(awg: FakeAwgServer) -> AwgServerConfig:
    return AwgServerConfig.parse(awg.container_files[AWG_CONFIG_PATH].decode())
