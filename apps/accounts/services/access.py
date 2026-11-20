"""Access of a staff member to the sections of the panel: none, view or manage.

The zone (`zone.py`) answers whose objects a member sees; this module answers which sections of
the panel they open and whether they may change anything there. A member's own choices are kept in
`User.access` (`{section: level}`); a section left out there gets the default of the role, and no
choice goes above the ceiling of the role. The administrator manages every section.
"""

from dataclasses import dataclass
from enum import IntEnum

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Role, User
from apps.accounts.services.zone import Zone


class Section(models.TextChoices):
    OVERVIEW = "overview", _("Overview")
    STATS = "stats", _("Statistics")
    SERVERS = "servers", _("Servers")
    BACKUPS = "backups", _("Backups")
    OPERATIONS = "operations", _("Operations")
    USERS = "users", _("Users")
    KEYS = "keys", _("Keys")
    REGISTRATION = "registration", _("Registration")
    PLANS = "plans", _("Plans")
    ORDERS = "orders", _("Orders")
    INBOX = "inbox", _("Inbox")
    ARTICLES = "articles", _("Articles")
    AUDIT = "audit", _("Audit log")
    STAFF = "staff", _("Staff")


class Level(IntEnum):
    NONE = 0
    VIEW = 1
    MANAGE = 2

    @classmethod
    def named(cls, name: str) -> "Level":
        """The level by its name in `User.access` and in templates: ``"view"``, ``"manage"``."""
        return cls[name.upper()]


class NotAllowedError(Exception):
    """A member tried to give a right above the ceiling of the role or above their own."""


@dataclass(frozen=True)
class Grant:
    """What a role gets in a section when nobody chose, and the most it can be given."""

    default: Level
    ceiling: Level


def _grants(**levels: tuple[Level, Level]) -> dict[str, Grant]:
    return {section: Grant(*pair) for section, pair in levels.items()}


_NONE, _VIEW, _MANAGE = Level.NONE, Level.VIEW, Level.MANAGE

# The moderator's defaults are exactly what a moderator could do in 1.0.0. An owner runs their
# own servers whole, sees the orders and the audit log of their zone, and may be given articles.
POLICIES: dict[str, dict[str, Grant]] = {
    Role.OWNER: _grants(
        overview=(_MANAGE, _MANAGE),
        stats=(_MANAGE, _MANAGE),
        servers=(_MANAGE, _MANAGE),
        backups=(_MANAGE, _MANAGE),
        operations=(_MANAGE, _MANAGE),
        users=(_MANAGE, _MANAGE),
        keys=(_MANAGE, _MANAGE),
        registration=(_MANAGE, _MANAGE),
        plans=(_MANAGE, _MANAGE),
        orders=(_VIEW, _VIEW),
        inbox=(_MANAGE, _MANAGE),
        articles=(_NONE, _MANAGE),
        audit=(_VIEW, _VIEW),
        staff=(_MANAGE, _MANAGE),
    ),
    Role.MODERATOR: _grants(
        overview=(_VIEW, _VIEW),
        stats=(_VIEW, _VIEW),
        servers=(_NONE, _VIEW),
        backups=(_NONE, _VIEW),
        operations=(_MANAGE, _MANAGE),
        users=(_MANAGE, _MANAGE),
        keys=(_MANAGE, _MANAGE),
        registration=(_MANAGE, _MANAGE),
        plans=(_NONE, _VIEW),
        orders=(_NONE, _NONE),
        inbox=(_MANAGE, _MANAGE),
        articles=(_NONE, _NONE),
        audit=(_NONE, _NONE),
        staff=(_NONE, _NONE),
    ),
}

_NO_GRANT = Grant(Level.NONE, Level.NONE)


class Access:
    """What `member` may do in each section of the panel."""

    def __init__(self, member: User) -> None:
        self._member = member

    def level(self, section: str) -> Level:
        if self._member.is_admin:
            return Level.MANAGE
        grant = self._grant(section)
        chosen = self._chosen(section)
        return min(grant.default if chosen is None else chosen, grant.ceiling)

    def allows(self, section: str, level: Level = Level.VIEW) -> bool:
        return self.level(section) >= level

    def ceiling(self, section: str) -> Level:
        """The most the member's role can be given in `section`."""
        return Level.MANAGE if self._member.is_admin else self._grant(section).ceiling

    def grant(self, target: User, section: str, level: Level) -> None:
        """Give `target` the `level` in `section`; not above the ceiling of their role, not above
        what the member has, and only to one of the member's moderators (`Zone.moderators`)."""
        if section not in Section.values:
            raise NotAllowedError(section)
        if not self._can_manage(target):
            raise NotAllowedError(section)
        if level > self.level(section) or level > Access(target).ceiling(section):
            raise NotAllowedError(section)
        target.access = {**target.access, section: level.name.lower()}
        target.save(update_fields=["access"])

    def _can_manage(self, target: User) -> bool:
        return (
            target.pk != self._member.pk
            and not target.is_admin
            and self.allows(Section.STAFF, Level.MANAGE)
            and Zone(self._member).moderators().filter(pk=target.pk).exists()
        )

    def _grant(self, section: str) -> Grant:
        return POLICIES.get(self._member.role, {}).get(section, _NO_GRANT)

    def _chosen(self, section: str) -> Level | None:
        name = (self._member.access or {}).get(section)
        try:
            return Level.named(name) if isinstance(name, str) else None
        except KeyError:
            return None
