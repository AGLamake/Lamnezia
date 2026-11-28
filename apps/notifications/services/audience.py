"""The staff members a routed event reaches in their browsers: the administrators hear of all of
it, a moderator or an owner of what happened in their zone, in a section of the panel they open.

An event names its server or its user in a detail (`server` is a server's name, `user` a
username); that detail decides whose zone it is in, and the section decides who may see it at all.
An event of no section here (relays and groups, reports, the panel's own archive) stays with the
administrators, as the objects it is about do (`Zone.groups`).
"""

from dataclasses import dataclass
from typing import ClassVar

from django.db.models import QuerySet

from apps.accounts.models import STAFF_ROLES, User
from apps.accounts.services.access import Access, Section
from apps.accounts.services.zone import Zone
from apps.notifications.events import Event
from apps.notifications.services.channels import Message


@dataclass(frozen=True)
class Scope:
    """The section an event belongs to and the detail that names its server or user."""

    section: Section
    detail: str

    @classmethod
    def server(cls, section: Section) -> "Scope":
        return cls(section, "server")

    @classmethod
    def user(cls, section: Section) -> "Scope":
        return cls(section, "user")


class StaffAudience:
    """Who of the active staff with a subscribed browser hears of `message`."""

    SCOPES: ClassVar[dict[Event, Scope]] = {
        Event.SERVER_DOWN: Scope.server(Section.SERVERS),
        Event.SERVER_UP: Scope.server(Section.SERVERS),
        Event.AWG3_DROPPED: Scope.server(Section.SERVERS),
        Event.AWG3_RESTORED: Scope.server(Section.SERVERS),
        Event.SERVER_LOAD_HIGH: Scope.server(Section.SERVERS),
        Event.SERVER_LOAD_NORMAL: Scope.server(Section.SERVERS),
        Event.ADDRESS_BUSY: Scope.server(Section.SERVERS),
        Event.ADDRESS_CALM: Scope.server(Section.SERVERS),
        Event.INTERFACE_LOST: Scope.server(Section.SERVERS),
        Event.INTERFACE_RESTORED: Scope.server(Section.SERVERS),
        Event.INTERFACE_REPAIR_FAILED: Scope.server(Section.SERVERS),
        Event.INCIDENT_OPENED: Scope.server(Section.INBOX),
        Event.INCIDENT_CLOSED: Scope.server(Section.INBOX),
        Event.KEY_CONNECTED: Scope.user(Section.KEYS),
        Event.KEYS_CHANGED: Scope.user(Section.KEYS),
        Event.KEYS_REVOKED: Scope.user(Section.KEYS),
        Event.KEYS_MOVED: Scope.user(Section.KEYS),
        Event.SHORT_ID_CHANGED: Scope.user(Section.KEYS),
        Event.KEYS_UPDATED: Scope.user(Section.KEYS),
        Event.KEYS_REISSUED: Scope.user(Section.KEYS),
        Event.LIMIT_REACHED: Scope.user(Section.USERS),
        Event.ACCESS_EXPIRING: Scope.user(Section.USERS),
        Event.TRAFFIC_LOW: Scope.user(Section.USERS),
        Event.SIGN_IN_NEW_PLACE: Scope.user(Section.USERS),
        Event.SIGN_IN_LOCKED: Scope.user(Section.USERS),
        Event.REGISTRATION_PENDING: Scope.user(Section.REGISTRATION),
        Event.TICKET_CREATED: Scope.user(Section.INBOX),
        Event.CHAT_MESSAGE: Scope.user(Section.INBOX),
        Event.SUGGESTION_CREATED: Scope.user(Section.ARTICLES),
        Event.PAYMENT_RECEIVED: Scope.user(Section.ORDERS),
    }

    def __init__(self, message: Message) -> None:
        self._message = message

    def members(self) -> list[User]:
        subscribed: QuerySet[User] = User.objects.filter(
            role__in=STAFF_ROLES, is_active=True, push_subscriptions__isnull=False
        ).distinct()
        return [member for member in subscribed.order_by("pk") if self.reaches(member)]

    def reaches(self, member: User) -> bool:
        if member.is_admin:
            return True
        scope = self.SCOPES.get(Event(self._message.event))
        if scope is None or not Access(member).allows(scope.section):
            return False
        named = self._message.data.get(scope.detail)
        if not named:
            return False
        zone = Zone(member)
        if scope.detail == "server":
            return zone.servers().filter(name=named).exists()
        return zone.users().filter(username=named).exists()
