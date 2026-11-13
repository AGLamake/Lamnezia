"""The `attention` tag: counters of what needs the signed-in staff member, for the admin menu."""

from typing import Any

from django import template

from apps.accounts.models import User
from apps.dashboard.services.attention import Attention

register = template.Library()


@register.simple_tag(takes_context=True)
def attention(context: dict[str, Any]) -> Attention | None:
    """The member's counters, made once per request; None for anyone who is not staff."""
    request = context["request"]
    user = request.user
    if not (isinstance(user, User) and user.is_staff_member):
        return None
    if not hasattr(request, "attention"):
        request.attention = Attention(user)
    found: Attention = request.attention
    return found
