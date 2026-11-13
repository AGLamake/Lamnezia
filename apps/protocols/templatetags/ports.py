"""Template tags of the protocol ports: the wiki page on ports and the speed test."""

from django import template

from apps.protocols.services.ports import LOW_PORT_LIMIT, ports_page_url

register = template.Library()


@register.simple_tag
def ports_wiki_url() -> str:
    """The wiki page on choosing the port, in the language of the page."""
    return ports_page_url()


@register.simple_tag
def low_port_limit() -> int:
    """The highest port the panel recommends for UDP."""
    return LOW_PORT_LIMIT
