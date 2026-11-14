"""Protocol instances as targets for keys and traffic; registered in `ProtocolsConfig.ready()`."""

from collections.abc import Iterable, Iterator
from functools import partial
from itertools import groupby

from amnezia_core.containers import WG_CONTAINERS, WgContainer
from amnezia_core.ipsec import IPSEC, IPSEC_CONTAINER, IpsecContainer
from amnezia_core.openvpn import OPENVPN, OPENVPN_CONTAINER, OpenVpnContainer
from amnezia_core.xray import XRAY, XRAY_CONTAINER, XrayContainer
from apps.keys.services.instances import Instance, instances
from apps.protocols.models import ProtocolInstance
from apps.servers.services.connection import connect
from apps.traffic.services.targets import PollTarget, poll_targets


def _instances(containers: Iterable[str], *, clones: bool = True) -> list[ProtocolInstance]:
    """Instances with a container on the server, grouped by server.

    A removed one has no clients to manage. Secrets are left unread: titles and lookups never
    decrypt them, and a connection loads the server's credentials when it opens. Without
    `clones`, group members other than the leading server are left out.
    """
    instances = ProtocolInstance.objects.filter(
        container__in=list(containers), container_removed_at__isnull=True
    )
    if not clones:
        instances = instances.exclude(group_member__leading=False)
    return list(instances.select_related("server").defer(*_SECRETS).order_by("server_id", "pk"))


_SECRETS = (
    "private_key",
    "preshared_key",
    "server__password",
    "server__private_key",
    "server__passphrase",
)


_KEY_CONTAINERS: dict[str, WgContainer | XrayContainer | OpenVpnContainer | IpsecContainer] = {
    **WG_CONTAINERS,
    XRAY_CONTAINER: XRAY,
    OPENVPN_CONTAINER: OPENVPN,
    IPSEC_CONTAINER: IPSEC,
}


@instances.register
def key_instances() -> Iterator[Instance]:
    # Keys of a group are issued on its leading server; the members are its clones.
    for instance in _instances(_KEY_CONTAINERS, clones=False):
        server = instance.server
        yield Instance(
            instance.source,
            f"{server.name} · {instance.title}",
            server.host,
            partial(connect, server),
            port=instance.port,
            server_id=server.pk,
            dns=instance.dns,
            container=_KEY_CONTAINERS[instance.container],
            short_id=str(instance.params.get("short_id", "")),
            server_ip=str(instance.params.get("server_ip", "")),
            openvpn_client_config=str(instance.params.get("additional_client_config", "")),
            block_outside_dns=instance.params.get("block_outside_dns") == "true",
            maintenance=server.maintenance,
        )


# Containers that report traffic per client: XRay only with the statistics blocks the panel adds.
_TRAFFIC_CONTAINERS = (*WG_CONTAINERS, XRAY_CONTAINER)


@poll_targets.register
def traffic_targets() -> Iterator[PollTarget]:
    by_server = groupby(_instances(_TRAFFIC_CONTAINERS), key=lambda instance: instance.server)
    for server, group in by_server:
        yield PollTarget(
            server.name,
            partial(connect, server),
            {instance.source: instance.container for instance in group},
        )
