"""The identity of an AmneziaWG 2.0 server changed by its files: they are replaced as a set, the
panel saves the key the server now runs with, and owners of its keys hear about it.

Shared by the groups (join, catch-up, leave) and the snapshot restore of the backups.
"""

from collections.abc import Iterable, Mapping
from contextlib import suppress

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from amnezia_core.awg import AwgServerConfig
from amnezia_core.awg_backup import AwgServerFiles
from amnezia_core.containers import AWG2
from amnezia_core.keys import KeyPair
from apps.keys.models import GONE_STATUSES, AccessKey, KeyStatus
from apps.notifications.events import Event
from apps.notifications.services.delivery import emit
from apps.operations.services.context import OperationContext
from apps.protocols.errors import SetupError
from apps.protocols.models import ProtocolInstance
from apps.protocols.services.awg_container import AwgContainer
from apps.protocols.services.awg_settings import kept_files
from apps.ssh.services import RemoteHost


class AwgFiles:
    """The amnezia-awg2 container of one server whose files are replaced as a set."""

    def __init__(self, host: RemoteHost, ctx: OperationContext) -> None:
        self._ctx = ctx
        self._docker = AwgContainer(host, ctx)

    def replace(self, files: Mapping[str, bytes]) -> None:
        """Write the files in order and restart the interface; if it does not come up, the old
        files come back.
        """
        shell = self._docker.shell
        previous = {
            path: shell.read_file(path) for path in kept_files(AWG2) if self._docker.has_file(path)
        }
        try:
            self._bring_up(files)
        except SetupError:
            with suppress(SetupError):
                self._bring_up(previous, check=False)
                self._ctx.log(_("The VPN interface runs with the old files again."))
            raise

    def write_clients_table(self, text: str) -> None:
        """Only the client list differs: it is written alone, the interface keeps running."""
        self._docker.shell.write_file_atomic(AWG2.clients_table_path, text.encode())

    def _bring_up(self, files: Mapping[str, bytes], *, check: bool = True) -> None:
        for path, data in files.items():
            self._docker.shell.write_file_atomic(path, data)
        self._docker.restart_awg(check=check)


@transaction.atomic
def take_identity(instance: ProtocolInstance, files: AwgServerFiles) -> list[AccessKey]:
    """Save the server key and settings the server now runs with; the keys it revoked.

    Keys issued for the old server key do not connect any more: they are revoked without a
    notice, the caller decides what their owners get instead.
    """
    config = AwgServerConfig.parse(files.config)
    private_key = config.private_key or ""
    revoked: list[AccessKey] = []
    if private_key != instance.private_key:
        keys = AccessKey.objects.filter(source=instance.source).exclude(status__in=GONE_STATUSES)
        revoked = list(keys.select_related("user").select_for_update(of=("self",)))
        keys.filter(pk__in=[key.pk for key in revoked]).update(
            status=KeyStatus.REVOKED, revoked_at=timezone.now()
        )
    instance.private_key = private_key
    instance.public_key = KeyPair.from_private_key(private_key).public_key
    instance.preshared_key = files.preshared_key
    instance.params = config.awg_params
    instance.port = config.listen_port or instance.port
    instance.subnet = str(config.address.network)
    instance.save(
        update_fields=["private_key", "public_key", "preshared_key", "params", "port", "subnet"]
    )
    return revoked


def tell_owners(keys: Iterable[AccessKey], server: str) -> int:
    """Tell every owner of `keys` once that their keys of `server` must be imported again."""
    owners = {key.user for key in keys if key.user is not None}
    for owner in owners:
        emit(Event.KEYS_UPDATED, {"user": owner.username, "server": server}, user=owner)
    return len(owners)
