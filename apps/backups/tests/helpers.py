from amnezia_core.awg_install import generate_awg_params
from amnezia_core.keys import KeyPair
from apps.keys.models import AccessKey, KeyStatus
from apps.protocols.models import InstanceOrigin, ProtocolInstance
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory

PSK = "u+xLktHw90auwUVtjZEJVpNCbfbFK8GNmGegY/Z0XYs="


def awg_instance(server: Server | None = None) -> ProtocolInstance:
    keys = KeyPair.generate()
    return ProtocolInstance.objects.create(
        server=server or ServerFactory(),
        container="amnezia-awg2",
        port=40001,
        subnet="10.9.8.0/24",
        params=generate_awg_params(),
        public_key=keys.public_key,
        private_key=keys.private_key,
        preshared_key=PSK,
        origin=InstanceOrigin.INSTALLED,
    )


def access_key(
    instance: ProtocolInstance, name: str, client_ip: str | None, status: str = KeyStatus.ACTIVE
) -> AccessKey:
    return AccessKey.objects.create(
        source=instance.source,
        name=name,
        public_key=KeyPair.generate().public_key,
        client_ip=client_ip,
        status=status,
    )
