"""Template tags of the protocols: the TSO/GSO card of a server."""

from typing import Any

from django import template

from apps.protocols.models import OffloadWorkaround
from apps.protocols.services import offload
from apps.servers.models import Server

register = template.Library()


@register.inclusion_tag("protocols/_offload.html", takes_context=True)
def offload_card(context: dict[str, Any], server: Server) -> dict[str, Any]:
    """The TSO/GSO workaround of a server with a WireGuard-family protocol or the workaround on."""
    workaround = OffloadWorkaround.objects.filter(server=server).first()
    shown = offload.applies(server) or bool(workaround and workaround.enabled)
    return {
        "server": server,
        "shown": shown,
        "workaround": workaround,
        "csrf_token": context.get("csrf_token"),
    }
