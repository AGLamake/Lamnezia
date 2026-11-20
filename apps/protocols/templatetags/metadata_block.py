"""Template tags of the protocols: the cloud metadata block card of a server."""

from typing import Any

from django import template

from apps.protocols.models import MetadataBlock
from apps.protocols.services import metadata_block
from apps.protocols.services.metadata_scripts import LINK_LOCAL
from apps.servers.models import Server

register = template.Library()


@register.inclusion_tag("protocols/_metadata_block.html", takes_context=True)
def metadata_card(context: dict[str, Any], server: Server) -> dict[str, Any]:
    """The cloud metadata block of a server with an Amnezia container or the block on."""
    block = MetadataBlock.objects.filter(server=server).first()
    shown = metadata_block.applies(server) or bool(block and block.enabled)
    return {
        "server": server,
        "shown": shown,
        "block": block,
        "network": LINK_LOCAL,
        "csrf_token": context.get("csrf_token"),
    }
