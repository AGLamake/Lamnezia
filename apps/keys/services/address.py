"""The server address in the profile of one key: the instance's own, or another one of its server.

If a provider blocks the server address for one user, their key can send them to another
checked address of the server (`Instance.addresses`). Only the profile changes: the peer on the
server stays as it is, the owner imports the key again. Keys of a server group have no other
address to choose: they go where the group sends them.
"""

from django.db import transaction

from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.keys.models import AccessKey
from apps.keys.services.instances import Instance, UnknownInstanceError, instances
from apps.keys.services.profile import profile_host, with_host
from apps.notifications.events import Event
from apps.notifications.services.delivery import emit

CHANGE_ADDRESS = "keys.change_address"


class KeyAddress:
    """The addresses the profile of `key` may carry, and moving it to one of them."""

    def __init__(self, key: AccessKey, actor: User, ip: str | None = None) -> None:
        self.key = key
        self._actor = actor
        self._ip = ip
        try:
            self._instance: Instance | None = instances.get(key.source)
        except UnknownInstanceError:
            self._instance = None

    @property
    def choices(self) -> list[str]:
        """The instance's own address first; none while the key has nothing to choose from."""
        instance, key = self._instance, self.key
        if instance is None or not instance.addresses or not key.profile or key.is_gone:
            return []
        return [instance.host, *instance.addresses]

    @property
    def current(self) -> str:
        return profile_host(self.key.profile)

    @transaction.atomic
    def change(self, host: str) -> bool:
        """Write `host` into the profile and tell the owner; False if it is there already."""
        if host not in self.choices:
            raise ValueError(host)
        if host == self.current:
            return False
        assert self._instance is not None
        key = self.key
        key.profile = with_host(key.profile, host)
        key.address = "" if host == self._instance.host else host
        key.save(update_fields=["profile", "address"])
        audit.log(CHANGE_ADDRESS, actor=self._actor, target=key, data={"host": host}, ip=self._ip)
        if key.user is not None:
            data = {"user": key.user.username, "server": self._instance.title}
            emit(Event.KEYS_CHANGED, data, user=key.user)
        return True
