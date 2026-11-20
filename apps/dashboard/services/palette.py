"""Quick search of the command palette (Ctrl+K): servers, users and keys of a member's zone.

Each group shows only when the member may open its section.
"""

from dataclasses import dataclass

from django.db.models import Q
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.accounts.models import User
from apps.accounts.services.access import Access, Section
from apps.accounts.services.zone import Zone
from apps.keys.services.zone import KeyZone
from apps.servers.models import Server

MIN_LENGTH = 2
_LIMIT = 6


@dataclass(frozen=True)
class Hit:
    title: str
    detail: str
    url: str


@dataclass(frozen=True)
class HitGroup:
    title: str
    hits: list[Hit]


class QuickSearch:
    """Finds what the member may open: the sections they have, within their zone."""

    def __init__(self, member: User) -> None:
        self._member = member
        self._access = Access(member)
        self._keys = KeyZone(member)

    def groups(self, text: str) -> list[HitGroup]:
        text = text.strip()
        if len(text) < MIN_LENGTH:
            return []
        searches = [
            (Section.SERVERS, _("Servers"), self._servers),
            (Section.USERS, _("Users"), self._users),
            (Section.KEYS, _("Keys"), self._keys_named),
        ]
        groups = [
            HitGroup(title, search(text))
            for section, title, search in searches
            if self._access.allows(section)
        ]
        return [group for group in groups if group.hits]

    def _servers(self, text: str) -> list[Hit]:
        found = Server.objects.filter(
            Q(name__icontains=text) | Q(host__icontains=text) | Q(location__icontains=text)
        )
        servers = Zone(self._member).narrow(found, server="pk").order_by("name")[:_LIMIT]
        return [
            Hit(server.name, server.host, reverse("servers:detail", args=[server.pk]))
            for server in servers
        ]

    def _users(self, text: str) -> list[Hit]:
        users = (
            self._keys.users()
            .filter(Q(username__icontains=text) | Q(email__icontains=text))
            .order_by("username")[:_LIMIT]
        )
        return [
            Hit(user.username, user.email, reverse("dashboard:user", args=[user.pk]))
            for user in users
        ]

    def _keys_named(self, text: str) -> list[Hit]:
        keys = (
            self._keys.keys()
            .filter(name__icontains=text)
            .select_related("user")
            .order_by("name")[:_LIMIT]
        )
        return [
            Hit(
                key.name,
                key.user.username if key.user else key.get_status_display(),
                reverse("keys:detail", args=[key.pk]),
            )
            for key in keys
        ]
