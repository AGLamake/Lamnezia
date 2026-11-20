"""The `can` tag (a level in a section) and the `whole_zone` tag, for menus and buttons."""

from typing import Any

from django import template

from apps.accounts.models import User
from apps.accounts.services.access import Access, Level
from apps.accounts.services.zone import Zone

register = template.Library()


@register.simple_tag(takes_context=True)
def can(context: dict[str, Any], section: str, level: str = "view") -> bool:
    """``{% can "servers" "manage" as ok %}``: the same answer the views of the section give."""
    user = context["request"].user
    return isinstance(user, User) and Access(user).allows(section, Level.named(level))


@register.simple_tag(takes_context=True)
def whole_zone(context: dict[str, Any]) -> bool:
    """``{% whole_zone as whole %}``: whether the member answers for the whole panel, so sees
    what nobody else answers for, such as server groups and relays."""
    user = context["request"].user
    return isinstance(user, User) and Zone(user).is_whole
