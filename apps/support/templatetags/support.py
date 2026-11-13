from typing import Any

from django import template

from apps.accounts.models import User
from apps.support.services.chat import MyChat
from apps.support.services.inbox import Inbox
from apps.support.services.tickets import MyRequests

register = template.Library()


@register.simple_tag(takes_context=True)
def inbox_unread(context: dict[str, Any]) -> int:
    """Unread messages in the inbox of the signed-in staff member, for the menu."""
    user = context["request"].user
    return Inbox(user).unread() if isinstance(user, User) and user.is_staff_member else 0


@register.simple_tag(takes_context=True)
def support_unread(context: dict[str, Any]) -> int:
    """Answers to the signed-in user's requests they have not seen, for the Support tab."""
    user = context["request"].user
    return MyRequests(user).unread() if isinstance(user, User) else 0


@register.simple_tag(takes_context=True)
def support_chat_unread(context: dict[str, Any]) -> int:
    """Chat answers the signed-in user has not seen, for the chat widget button."""
    user = context["request"].user
    return MyChat(user).unread() if isinstance(user, User) else 0
