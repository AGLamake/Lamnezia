"""Template tags of the I1 masking: the template choices of the move to AmneziaWG 3.1."""

from django import template
from django.utils.functional import Promise

from apps.protocols.masking import RANDOM, TEMPLATES

register = template.Library()


@register.simple_tag
def masking_templates() -> tuple[tuple[str, Promise], ...]:
    """(value, label) of every I1 template, the random one first."""
    return TEMPLATES


@register.simple_tag
def masking_default() -> str:
    """The choice a new server gets unless the administrator picks another."""
    return RANDOM
