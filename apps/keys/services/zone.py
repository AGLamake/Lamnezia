"""Keys within a staff member's zone: a moderator's users' keys on the moderator's servers."""

from django.db import transaction
from django.db.models import QuerySet

from apps.accounts.models import Role, Status, User
from apps.accounts.services.zone import Zone
from apps.keys.models import GONE_STATUSES, AccessKey, Drift
from apps.keys.services.actions import KeyActions
from apps.keys.services.instances import instances
from apps.operations.models import Operation


class KeyQuotaReachedError(Exception):
    """A moderator's users already hold as many keys as the moderator's quota allows."""


class KeyZone:
    """An administrator gets every key and instance; a moderator only those of their zone."""

    def __init__(self, member: User) -> None:
        self._member = member
        self._zone = Zone(member)

    @property
    def is_whole(self) -> bool:
        return self._zone.is_whole

    def users(self) -> QuerySet[User]:
        return self._zone.users()

    def titles(self) -> dict[str, str]:
        """Instances of the zone, by source."""
        return {
            instance.source: instance.title
            for instance in instances
            if self._zone.has_server(instance.server_id)
        }

    def issue_titles(self) -> dict[str, str]:
        """Instances a new key may be issued on: those of servers not under maintenance."""
        return {
            instance.source: instance.title
            for instance in instances
            if self._zone.has_server(instance.server_id) and not instance.maintenance
        }

    def keys(self) -> QuerySet[AccessKey]:
        keys = AccessKey.objects.all()
        if self._zone.is_whole:
            return keys
        return keys.filter(user__in=self._zone.users(), source__in=list(self.titles()))

    def drifts(self) -> QuerySet[Drift]:
        drifts = Drift.objects.all()
        if self._zone.is_whole:
            return drifts
        return drifts.filter(source__in=list(self.titles()))

    def owners(self) -> QuerySet[User]:
        """Users a key can be issued to or handed over to."""
        users = self._zone.users().filter(is_active=True, status=Status.ACTIVE)
        return users.order_by("username")

    def quota_reached(self) -> bool:
        """A moderator's users hold as many keys as the moderator's quota allows."""
        return not self._zone.is_whole and KeyQuota(self._member).reached()

    def issue(self, actions: KeyActions, source: str, name: str, owner: User) -> Operation:
        """Start issuing a key to `owner`, within a moderator's quota.

        The moderator's row stays locked until commit, so two issues at once, from here or from
        the portal of their users, cannot both pass the quota.
        """
        with transaction.atomic():
            if not self._zone.is_whole:
                moderator = User.objects.select_for_update().get(pk=self._member.pk)
                if KeyQuota(moderator).reached():
                    raise KeyQuotaReachedError
            return actions.issue(source, name, owner)


class KeyQuota:
    """The keys the users of moderators may hold together (`User.key_quota` of each); revoked
    ones do not count. A key of a user served by several moderators counts for each of them, so
    the quota is reached once any of them has reached theirs. A user without a moderator, or a
    moderator without a quota, has no such limit.
    """

    def __init__(self, *moderators: User) -> None:
        self._moderators = moderators

    @classmethod
    def of(cls, user: User) -> "KeyQuota":
        """The quotas of the moderators who serve `user`."""
        return cls(*user.managers.all())

    def reached(self) -> bool:
        return any(self._reached(moderator) for moderator in self._moderators)

    @staticmethod
    def _reached(moderator: User) -> bool:
        if moderator.role != Role.MODERATOR or moderator.key_quota is None:
            return False
        held = AccessKey.objects.filter(user__managers=moderator)
        return held.exclude(status__in=GONE_STATUSES).count() >= moderator.key_quota
