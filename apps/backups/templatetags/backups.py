"""Template tags of the backups: the backup card of a server and the restore check of a protocol."""

from typing import Any

from django import template

from amnezia_core.ipsec import IPSEC_CONTAINER
from apps.backups.services import snapshot
from apps.backups.services.archived_services import archived_services
from apps.protocols.models import ProtocolInstance, ServiceInstance
from apps.protocols.services.ipsec_reissue import IpsecIdentity
from apps.servers.models import Server

register = template.Library()


@register.inclusion_tag("backups/_server.html", takes_context=True)
def protocol_backups(context: dict[str, Any], server: Server) -> dict[str, Any]:
    """Saved copies of the server protocols and services with the restore buttons."""
    instances = ProtocolInstance.objects.filter(
        server=server, container__in=snapshot.backed_up_containers()
    ).select_related("backup")
    services = ServiceInstance.objects.filter(
        server=server, container__in=archived_services.containers()
    ).select_related("backup")
    return {
        "instances": list(instances),
        "services": list(services),
        "csrf_token": context.get("csrf_token"),
    }


@register.filter
def restorable(instance: ProtocolInstance) -> bool:
    """Whether the keys of the protocol can work again after its container is removed."""
    return snapshot.restorable(instance)


@register.filter
def ipsec_identity(instance: ProtocolInstance) -> IpsecIdentity | None:
    """The IP the IPsec server certificate is issued for; None for the other protocols."""
    return IpsecIdentity(instance) if instance.container == IPSEC_CONTAINER else None


@register.filter
def service_restorable(service: ServiceInstance) -> bool:
    """Whether the panel has a copy to install the service again from."""
    return snapshot.service_restorable(service)
