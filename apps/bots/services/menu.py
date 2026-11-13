"""The commands a messenger lists in its command menu, the same for every bot."""

from dataclasses import dataclass

from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _


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
