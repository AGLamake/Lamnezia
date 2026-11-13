"""Move of the clients of a legacy AmneziaWG container (amnezia-awg) to amnezia-awg2 on 3.1.

The Amnezia app has no such move: legacy clients would lose access at a reinstall. The panel puts
amnezia-awg2 next to the old container, if the server has none, and gives every client a new key
there in one change of awg0.conf and clientsTable (DESIGN.md 5.4); the owners are told to import
the new keys. The old container keeps serving until the administrator confirms the end of the move:
then it is removed and its keys are revoked. Running the move again picks up the keys issued on the
old container meanwhile.
"""

from dataclasses import dataclass
from functools import partial

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone
from django.utils.translation import gettext as _

from amnezia_core.containers import AWG2, AWG_LEGACY
from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.keys.errors import PeerError, PeerErrorCode
from apps.keys.models import DISABLED_STATUSES, AccessKey, KeyStatus
from apps.keys.services import actions
from apps.keys.services.backends import (
    KeyBackend,
    Reissue,
    ReissueOutcome,
    WgKeys,
    new_credentials,
)
from apps.keys.services.instances import instances
from apps.keys.services.peers import PeerManager
from apps.keys.services.reissue import pending
from apps.keys.signals import clients_adding, keys_written
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.protocols.errors import SETUP_ERRORS, SetupError, SetupErrorCode
from apps.protocols.models import AwgMigration, InstanceOrigin, ProtocolInstance
from apps.protocols.services.importer import ServerImporter
from apps.protocols.services.installation import used_ports
from apps.protocols.services.installer import installers
from apps.protocols.services.lifecycle import ContainerLifecycle, RemovedInstance
from apps.protocols.signals import keys_moved
from apps.ssh.services import RemoteHost

MIGRATE_KIND = "protocols.migrate_awg"
FINISH_KIND = "protocols.finish_awg_migration"

# Keys a client can connect with, or come back with: they are the ones moved.
_LIVE_STATUSES = (KeyStatus.ACTIVE, *DISABLED_STATUSES)


def can_migrate(instance: ProtocolInstance) -> bool:
    """A legacy AmneziaWG container that is still on the server."""
    return instance.container == AWG_LEGACY.name and not instance.removed


def target_of(legacy: ProtocolInstance) -> ProtocolInstance | None:
    """amnezia-awg2 of the server of `legacy`, if the panel knows one."""
    return ProtocolInstance.objects.filter(server=legacy.server, container=AWG2.name).first()


def blocker(legacy: ProtocolInstance) -> SetupErrorCode | None:
    """Why the clients cannot be moved now; None when they can."""
    target = target_of(legacy)
    if target is None:
        return None
    if target.removed:
        return SetupErrorCode.MIGRATION_TARGET_REMOVED
    if target.awg_outdated:
        return SetupErrorCode.MIGRATION_TARGET_OUTDATED
    return None


def unmoved_keys(legacy: ProtocolInstance) -> QuerySet[AccessKey]:
    """Live keys of `legacy` with no new key yet."""
    migration = AwgMigration.objects.filter(legacy=legacy).first()
    moved = migration.old_key_ids if migration else set()
    return AccessKey.objects.filter(source=legacy.source, status__in=_LIVE_STATUSES).exclude(
        pk__in=moved
    )


def suggested_port(legacy: ProtocolInstance) -> int:
    """The port amnezia-awg2 is installed on when the server has none."""
    return installers.get(AWG2.name).suggested_port(used_ports(legacy.server))


def start_migration(
    legacy: ProtocolInstance, user: User, *, port: int, ip: str | None = None
) -> Operation:
    """`port` is for amnezia-awg2 if it has to be installed."""
    params = {"instance_id": legacy.pk, "user_id": user.pk, "port": port}
    operation = launch(MIGRATE_KIND, params, user=user, subject=str(legacy))
    audit.log(MIGRATE_KIND, actor=user, target=legacy, data={"operation": operation.pk}, ip=ip)
    return operation


def start_finish(legacy: ProtocolInstance, user: User, *, ip: str | None = None) -> Operation:
    params = {"instance_id": legacy.pk, "user_id": user.pk}
    operation = launch(FINISH_KIND, params, user=user, subject=str(legacy))
    audit.log(FINISH_KIND, actor=user, target=legacy, data={"operation": operation.pk}, ip=ip)
    return operation


@dataclass(frozen=True)
class MigrationState:
    """What the server card shows on a legacy container: the move, if begun, and what is left."""

    migration: AwgMigration | None
    unmoved: int
    # The port amnezia-awg2 gets; None when the server has it already.
    port: int | None
    blocker: SetupErrorCode | None

    @classmethod
    def of(cls, legacy: ProtocolInstance) -> "MigrationState":
        return cls(
            AwgMigration.objects.filter(legacy=legacy).first(),
            unmoved_keys(legacy).count(),
            None if target_of(legacy) else suggested_port(legacy),
            blocker(legacy),
        )

    @property
    def blocked(self) -> str:
        """Why the clients cannot be moved now, for the administrator; empty when they can."""
        return str(SETUP_ERRORS[self.blocker].message) if self.blocker else ""

    @property
    def moved(self) -> int:
        return len(self.migration.pairs) if self.migration else 0

    @property
    def can_finish(self) -> bool:
        """Every client has a new key: the old container may go."""
        return bool(self.migration and self.migration.moved_at) and not self.unmoved


class AwgMigrationRun:
    """One run of the move on a connected server; the legacy container is not touched."""

    def __init__(self, legacy: ProtocolInstance, host: RemoteHost, ctx: OperationContext) -> None:
        self._legacy = legacy
        self._host = host
        self._ctx = ctx

    def run(self, port: int) -> None:
        reason = blocker(self._legacy)
        if reason:
            raise SetupError(reason)
        target = target_of(self._legacy) or self._install(port)
        with self._ctx.step(_("Plan the new keys")):
            pairs = self._plan(target)
        outcome = self._issue(target, pairs) if pairs else ReissueOutcome()
        self._save(target, outcome)
        if outcome.failed:
            raise PeerError(
                PeerErrorCode.NOT_ALL_REISSUED, f"{len(outcome.failed)} of {len(pairs)}"
            )

    def _install(self, port: int) -> ProtocolInstance:
        """amnezia-awg2 of the panel: a new server gets the parameters of 3.1."""
        params = {"port": port, "logs": self._legacy.container_logs}
        installers.get(AWG2.name).from_params(self._host, self._ctx, params).run()
        server = self._legacy.server
        ServerImporter(server, self._host, self._ctx, origin=InstanceOrigin.INSTALLED).run()
        target = target_of(self._legacy)
        if target is None:
            raise SetupError(SetupErrorCode.MIGRATION_TARGET_MISSING)
        return target

    @transaction.atomic
    def _plan(self, target: ProtocolInstance) -> list[Reissue]:
        """A new key for every live key with none yet; the pairs whose new key is still to issue.

        The migration row is locked, so two runs never make two new keys for one client.
        """
        AwgMigration.objects.get_or_create(legacy=self._legacy)
        migration = AwgMigration.objects.select_for_update().get(legacy=self._legacy)
        source = instances.get(target.source)
        for old in unmoved_keys(self._legacy).order_by("pk"):
            public_key, private_key = new_credentials(source)
            new = AccessKey.objects.create(
                user=old.user,
                source=target.source,
                name=old.name,
                public_key=public_key,
                private_key=private_key,
            )
            migration.pairs.append([old.pk, new.pk])
        migration.save(update_fields=["pairs"])
        pairs = pending(migration.pairs)
        for pair in [pair for pair in pairs if pair.old.status == KeyStatus.REVOKED]:
            _revoke(pair.new)
            pairs.remove(pair)
        return pairs

    def _issue(self, target: ProtocolInstance, pairs: list[Reissue]) -> ReissueOutcome:
        public_keys = [pair.new.public_key for pair in pairs]
        clients_adding.send(KeyBackend, source=target.source, public_keys=public_keys)
        outcome = self._keys(target).move(pairs)
        keys_written.send(KeyBackend, source=target.source)
        return outcome

    def _save(self, target: ProtocolInstance, outcome: ReissueOutcome) -> None:
        """The new keys of disabled ones are disabled too, so they come back with them."""
        with self._ctx.step(_("Save the keys")):
            with transaction.atomic():
                for pair in outcome.done:
                    pair.new.status = KeyStatus.ACTIVE
                    pair.new.save(update_fields=["client_ip", "profile", "status"])
                self._tell_owners([pair.new for pair in outcome.done])
            self._ctx.log(_("Keys moved: %(count)d.") % {"count": len(outcome.done)})
            for pair, reason in outcome.failed:
                self._ctx.log(
                    _("Key %(key)s is not moved: %(reason)s")
                    % {"key": actions.key_subject(pair.old), "reason": reason}
                )
            if not outcome.failed:
                AwgMigration.objects.filter(legacy=self._legacy).update(moved_at=timezone.now())
                self._ctx.log(_("Every client has a key on AmneziaWG 3.1."))
        disabled = [pair for pair in outcome.done if pair.old.status in DISABLED_STATUSES]
        if disabled:
            keys = self._keys(target)
            for pair in disabled:
                keys.disable(pair.new)
                pair.new.status = pair.old.status
                pair.new.save(update_fields=["status"])

    def _tell_owners(self, keys: list[AccessKey]) -> None:
        owners = User.objects.filter(access_keys__in=keys).distinct()
        server = self._legacy.server.name
        for owner in owners:
            transaction.on_commit(partial(keys_moved.send, type(self), user=owner, server=server))
        self._ctx.log(_("Owners notified: %(count)d.") % {"count": len(owners)})

    def _keys(self, target: ProtocolInstance) -> WgKeys:
        return WgKeys(instances.get(target.source), AWG2, PeerManager(self._host, self._ctx, AWG2))


class LegacyRetirement:
    """The end of the move: the old container goes, its keys are revoked.

    The moved keys are revoked quietly, their owners have new ones; owners of keys issued after
    the last run hear that their keys are revoked.
    """

    def __init__(self, legacy: ProtocolInstance, host: RemoteHost, ctx: OperationContext) -> None:
        self._legacy = legacy
        self._host = host
        self._ctx = ctx

    def run(self) -> None:
        state = MigrationState.of(self._legacy)
        if not state.can_finish or state.migration is None:
            raise SetupError(SetupErrorCode.MIGRATION_NOT_DONE)
        migration = state.migration
        if not self._legacy.removed:
            ContainerLifecycle(self._host, self._legacy, self._ctx).remove()
        with self._ctx.step(_("Revoke the old keys")), transaction.atomic():
            moved = AccessKey.objects.filter(
                source=self._legacy.source, pk__in=migration.old_key_ids
            ).exclude(status=KeyStatus.REVOKED)
            for key in moved:
                _revoke(key)
            RemovedInstance(self._legacy, self._ctx).forget()


def _revoke(key: AccessKey) -> None:
    """Revoked for good, so its secrets are not kept."""
    key.status, key.revoked_at = KeyStatus.REVOKED, timezone.now()
    key.private_key = key.profile = ""
    key.save(update_fields=["status", "revoked_at", "private_key", "profile"])
