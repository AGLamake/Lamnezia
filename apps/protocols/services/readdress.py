"""Keys after the server address changes: profiles moved to the new address, imported keys reissued.

vpn:// carries the server address. Keys issued by the panel keep the server half of the profile,
so the new address is written into it and the owner imports the key again. Imported keys have no
profile; they are reissued, if the administrator asks. A key sent to another checked address of
the server keeps it; once that address is removed, the key moves to the server's own one. Keys
of a group's leading server behind a running relay stay on the address of the group.
"""

from dataclasses import dataclass
from functools import partial

from django.db import transaction

from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.instances import instances
from apps.keys.services.profile import profile_host, with_host
from apps.keys.services.reissue import KeyReissue
from apps.protocols.models import ProtocolInstance
from apps.protocols.signals import keys_changed
from apps.servers.models import Server

READDRESS = "protocols.readdress"


@dataclass(frozen=True)
class ReaddressResult:
    moved: int
    reissued: int
    kept: int
    owners: int


class ServerReaddress:
    """The server's live keys that do not point where they should: its address or their own."""

    def __init__(self, server: Server) -> None:
        self.server = server
        keys = (
            AccessKey.objects.filter(source__in=ProtocolInstance.sources_of(server))
            .exclude(status=KeyStatus.REVOKED)
            .select_related("user")
        )
        self._addresses = set(
            server.addresses.filter(verified_at__isnull=False).values_list("host", flat=True)
        )
        sources = set(ProtocolInstance.sources_of(server))
        self._instances = {
            instance.source: instance for instance in instances if instance.source in sources
        }
        self.outdated = [
            key for key in keys if key.profile and profile_host(key.profile) != self._host(key)
        ]
        self.imported = [key for key in keys if key.imported]

    @property
    def pending(self) -> bool:
        return bool(self.outdated or self.imported)

    @transaction.atomic
    def apply(self, actor: User, *, reissue: bool, ip: str | None = None) -> ReaddressResult:
        """Move the profiles, reissue imported keys if asked, and tell the owners."""
        for key in self.outdated:
            host = self._host(key)
            key.address = "" if host == self._own_host(key) else host
            key.profile = with_host(key.profile, host)
            key.save(update_fields=["profile", "address"])
        reissued = self._reissue(actor, ip) if reissue else []
        owners = {key.user for key in [*self.outdated, *reissued] if key.user is not None}
        for owner in owners:
            transaction.on_commit(
                partial(keys_changed.send, type(self), user=owner, server=self.server.name)
            )
        result = ReaddressResult(
            moved=len(self.outdated),
            reissued=len(reissued),
            kept=len(self.imported) - len(reissued),
            owners=len(owners),
        )
        audit.log(
            READDRESS,
            actor=actor,
            target=self.server,
            data={"host": self.server.host, "moved": result.moved, "reissued": result.reissued},
            ip=ip,
        )
        return result

    def _host(self, key: AccessKey) -> str:
        """Where the key should send clients, as a newly issued key of its instance would."""
        instance = self._instances.get(key.source)
        if instance is not None:
            return instance.client_host(key.address)
        return key.address if key.address in self._addresses else self.server.host

    def _own_host(self, key: AccessKey) -> str:
        """The instance's address; the server's for a key of a removed container."""
        instance = self._instances.get(key.source)
        return instance.host if instance is not None else self.server.host

    def _reissue(self, actor: User, ip: str | None) -> list[AccessKey]:
        """One operation per protocol of the server, however many keys it has."""
        return KeyReissue(actor, ip).start(self.imported).reissued
