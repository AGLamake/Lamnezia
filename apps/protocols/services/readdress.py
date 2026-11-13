"""Keys after the server address changes: profiles moved to the new address, imported keys reissued.

vpn:// carries the server address. Keys issued by the panel keep the server half of the profile,
so the new address is written into it and the owner imports the key again. Imported keys have no
profile; they are reissued, if the administrator asks.
"""

import json
from dataclasses import dataclass
from functools import partial

from django.db import transaction

from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.keys.models import AccessKey, KeyStatus
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
    """The server's live keys that still point at its old address."""

    def __init__(self, server: Server) -> None:
        self.server = server
        keys = (
            AccessKey.objects.filter(source__in=ProtocolInstance.sources_of(server))
            .exclude(status=KeyStatus.REVOKED)
            .select_related("user")
        )
        self.outdated = [key for key in keys if key.profile and _host(key) != server.host]
        self.imported = [key for key in keys if key.imported]

    @property
    def pending(self) -> bool:
        return bool(self.outdated or self.imported)

    @transaction.atomic
    def apply(self, actor: User, *, reissue: bool, ip: str | None = None) -> ReaddressResult:
        """Move the profiles, reissue imported keys if asked, and tell the owners."""
        for key in self.outdated:
            key.profile = _with_host(key.profile, self.server.host)
            key.save(update_fields=["profile"])
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

    def _reissue(self, actor: User, ip: str | None) -> list[AccessKey]:
        """One operation per protocol of the server, however many keys it has."""
        return KeyReissue(actor, ip).start(self.imported).reissued


def _host(key: AccessKey) -> str:
    return str(json.loads(key.profile).get("host", ""))


def _with_host(profile: str, host: str) -> str:
    """Every key profile keeps the address in `host` and is saved as sorted JSON."""
    return json.dumps({**json.loads(profile), "host": host}, sort_keys=True)
