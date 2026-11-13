"""The zone of a staff member: the users and servers they answer for.

An administrator's zone is the whole panel. A moderator's zone is the users they serve
(`User.manager`) and the servers given to them (`User.moderated_servers`); everything else is
as good as missing for them.
"""

from functools import cached_property

from django.db.models import Model, QuerySet

from apps.accounts.models import User


class Zone:
    def __init__(self, member: User) -> None:
        self._member = member

    @property
    def is_whole(self) -> bool:
        return self._member.is_admin

    def users(self) -> QuerySet[User]:
        if self.is_whole:
            return User.objects.all()
        return User.objects.filter(manager=self._member)

    def narrow[M: Model](
        self, items: QuerySet[M], *, user: str | None = None, server: str | None = None
    ) -> QuerySet[M]:
        """`items` within the zone; `user` and `server` are the lookups that lead to their owner
        and server, e.g. ``narrow(conversations, user="user")`` or ``narrow(servers, server="pk")``.
        """
        if self.is_whole:
            return items
        if user is not None:
            items = items.filter(**{f"{user}__in": self.users()})
        if server is not None:
            items = items.filter(**{f"{server}__in": self._member.moderated_servers.all()})
        return items

    def has_server(self, server_id: int | None) -> bool:
        """Instances that run on no panel server are for administrators only."""
        return self.is_whole or server_id in self._server_ids

    @cached_property
    def _server_ids(self) -> frozenset[int]:
        return frozenset(self._member.moderated_servers.values_list("pk", flat=True))
