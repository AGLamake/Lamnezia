"""The `can` tag: whether the signed-in member has a level in a section, for menus and buttons."""

from typing import Any

from django import template

from apps.accounts.models import User
from apps.accounts.services.access import Access, Level

register = template.Library()


@register.simple_tag(takes_context=True)
def can(context: dict[str, Any], section: str, level: str = "view") -> bool:
    """``{% can "servers" "manage" as ok %}``: the same answer the views of the section give."""
    user = context["request"].user
    return isinstance(user, User) and Access(user).allows(section, Level.named(level))
