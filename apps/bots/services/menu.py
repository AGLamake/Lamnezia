"""The commands a messenger lists in its command menu, the same for every bot.

Staff members also get the staff commands in their own chats with the bot.
"""

from dataclasses import dataclass

from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import STAFF_ROLES, LinkedAccount


@dataclass(frozen=True)
class MenuCommand:
    name: str
    description: Promise


MENU: tuple[MenuCommand, ...] = (
    MenuCommand("menu", _("Menu")),
    MenuCommand("status", _("Status")),
    MenuCommand("keys", _("My keys")),
    MenuCommand("newkey", _("New key")),
    MenuCommand("sub", _("Subscription link")),
    MenuCommand("servers", _("Servers")),
    MenuCommand("down", _("A server does not work")),
    MenuCommand("support", _("Support")),
    MenuCommand("ticket", _("New request")),
    MenuCommand("requests", _("My requests")),
    MenuCommand("guides", _("Guides")),
    MenuCommand("unlink", _("Unlink")),
)

STAFF_MENU: tuple[MenuCommand, ...] = (
    MenuCommand("admin", _("Administration")),
    MenuCommand("overview", _("Server summary")),
    MenuCommand("today", _("Traffic today")),
    MenuCommand("incidents", _("Incidents")),
)


@dataclass(frozen=True)
class StaffChat:
    """A staff member's chat with the bot and the language of their menu."""

    external_id: str
    language: str


def staff_chats(messenger: str) -> list[StaffChat]:
    """The chats of the staff members with the bot of `messenger`."""
    accounts = LinkedAccount.objects.filter(
        messenger=messenger, user__role__in=STAFF_ROLES
    ).select_related("user")
    return [
        StaffChat(account.external_id, account.user.notification_language) for account in accounts
    ]
