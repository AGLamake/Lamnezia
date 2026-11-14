"""Operation handlers of keys. Each one is idempotent: a failed operation can be run again."""

from collections.abc import Iterator
from contextlib import contextmanager

from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from amnezia_core.awg_peers import AwgPeerRegistry
from amnezia_core.containers import WgContainer
from apps.keys.errors import PeerError, PeerErrorCode
from apps.keys.models import DISABLED_STATUSES, AccessKey, Drift, DriftKind, KeyStatus
from apps.keys.services import actions, drift, limits, reissue, xray_profiles
from apps.keys.services.adoption import adopt_peer
from apps.keys.services.backends import KeyBackend, ReissueOutcome, WgKeys, backend_for
from apps.keys.services.instances import Instance, instances
from apps.keys.services.issued import IssuedKeys
from apps.keys.services.peers import PeerManager, read_registry
from apps.keys.services.replicas import GroupKeys, key_groups
from apps.keys.signals import clients_adding, keys_written
from apps.operations.services.context import OperationContext
from apps.operations.services.registry import operations
from apps.operations.services.subjects import subjects

# Name of an accepted peer that has none in clientsTable.
_ADDED_IN_APP = _("Added in the Amnezia app")

# A key operation is about the key, not its server.
subjects.register("key_id", lambda pk: reverse("keys:detail", args=[pk]), priority=10)


@contextmanager
def _backend(ctx: OperationContext, source: str) -> Iterator[KeyBackend]:
    """The backend of the instance; once its change is on the server, others are told.

    A key of a server group is changed on every member of the group that answers.
    """
    instance = instances.get(source)
    group = key_groups.of(source)
    if group is None:
        with instance.connect() as host:
            yield backend_for(instance, host, ctx)
    else:
        try:
            with GroupKeys.connect(instance, group, ctx) as backend:
                yield backend
        finally:
            group.finish()
    keys_written.send(KeyBackend, source=instance.source)


def _key(ctx: OperationContext) -> AccessKey:
    return AccessKey.objects.get(pk=ctx.params["key_id"])


def _set_status(key: AccessKey, status: KeyStatus) -> None:
    key.status = status
    key.save(update_fields=["status"])


@operations.register(actions.ISSUE, _("Issue key"))
def issue(ctx: OperationContext) -> None:
    key = _key(ctx)
    clients_adding.send(KeyBackend, source=key.source, public_keys=[key.public_key])
    with _backend(ctx, key.source) as backend:
        backend.issue(key)
    key.status = KeyStatus.ACTIVE
    IssuedKeys([key]).save()


@operations.register(actions.DISABLE, _("Disable a key"))
def disable(ctx: OperationContext) -> None:
    key = _key(ctx)
    with _backend(ctx, key.source) as backend:
        backend.disable(key)
    _set_status(key, KeyStatus.DISABLED_MANUAL)


@operations.register(actions.ENABLE, _("Enable a key"))
def enable(ctx: OperationContext) -> None:
    key = _key(ctx)
    with _backend(ctx, key.source) as backend:
        backend.enable(key)
    _set_status(key, KeyStatus.ACTIVE)


@operations.register(limits.LIMIT_DISABLE, _("Disable a key over the limit"))
def limit_disable(ctx: OperationContext) -> None:
    """A key turned off by hand or revoked since the start is left as it is."""
    key = _key(ctx)
    if key.status != KeyStatus.ACTIVE:
        ctx.log(str(_("The key is no longer active.")))
        return
    with _backend(ctx, key.source) as backend:
        backend.disable(key)
    _move_status(key, KeyStatus.ACTIVE, KeyStatus.DISABLED_LIMIT)


@operations.register(limits.LIMIT_ENABLE, _("Enable a key within the limits"))
def limit_enable(ctx: OperationContext) -> None:
    key = _key(ctx)
    if key.status != KeyStatus.DISABLED_LIMIT:
        ctx.log(str(_("The key is no longer disabled by the limits.")))
        return
    with _backend(ctx, key.source) as backend:
        backend.enable(key)
        if not _move_status(key, KeyStatus.DISABLED_LIMIT, KeyStatus.ACTIVE):
            # Turned off by hand or revoked meanwhile: that must not be undone. The UUID of an
            # XRay key is cleared on revoke, so the key read before is used to reach it.
            if AccessKey.objects.get(pk=key.pk).is_gone:
                backend.revoke(key)
            else:
                backend.disable(key)


def _move_status(key: AccessKey, current: KeyStatus, new: KeyStatus) -> bool:
    """Change the status only if nothing else changed it during the operation."""
    return AccessKey.objects.filter(pk=key.pk, status=current).update(status=new) == 1


@operations.register(actions.REVOKE, _("Revoke a key"))
def revoke(ctx: OperationContext) -> None:
    key = _key(ctx)
    with _backend(ctx, key.source) as backend:
        backend.revoke(key)
    _forget(key)


@operations.register(reissue.REISSUE, _("Reissue keys"))
def reissue_keys(ctx: OperationContext) -> None:
    """New keys in place of old ones of one instance, in one change of the server files.

    A key that could not be reissued keeps its old one, which gets its status back, and is
    named in the log; the operation then fails, and running it again retries only the keys
    still left.
    """
    pairs = reissue.pending(ctx.params["pairs"])
    outcome = ReissueOutcome()
    if pairs:
        new_keys = [pair.new.public_key for pair in pairs]
        clients_adding.send(KeyBackend, source=ctx.params["source"], public_keys=new_keys)
        with _backend(ctx, ctx.params["source"]) as backend:
            outcome = backend.reissue(pairs)
    with ctx.step(_("Save the keys")), transaction.atomic():
        for pair in outcome.done:
            pair.new.status = KeyStatus.ACTIVE
        IssuedKeys([pair.new for pair in outcome.done]).save()
        for pair in outcome.done:
            if pair.old.status != KeyStatus.REVOKED:
                _forget(pair.old)
        reissue.give_back(ctx.params["pairs"], [pair.old for pair in outcome.done])
        ctx.log(_("Keys reissued: %(count)d.") % {"count": len(outcome.done)})
        for pair, reason in outcome.failed:
            ctx.log(
                _("Key %(key)s is not reissued: %(reason)s")
                % {"key": actions.key_subject(pair.old), "reason": reason}
            )
    if outcome.failed:
        raise PeerError(PeerErrorCode.NOT_ALL_REISSUED, f"{len(outcome.failed)} of {len(pairs)}")


def _forget(key: AccessKey) -> None:
    """Mark the key revoked; it can never be used again, so its secrets are not kept."""
    key.status, key.revoked_at = KeyStatus.REVOKED, timezone.now()
    key.private_key = key.profile = ""
    key.save(update_fields=["status", "revoked_at", "private_key", "profile"])


@operations.register(actions.ASSIGN, _("Give a key to a user"))
def assign(ctx: OperationContext) -> None:
    key = _key(ctx)
    key.user_id = ctx.params["owner_id"]
    # A disabled key gets its owner on the server when it is enabled again.
    if key.status == KeyStatus.ACTIVE:
        with _backend(ctx, key.source) as backend:
            backend.assign(key)
    key.save(update_fields=["user"])


@operations.register(actions.RENAME, _("Rename a key"))
def rename(ctx: OperationContext) -> None:
    key = _key(ctx)
    key.name = ctx.params["name"]
    # A disabled key keeps its clientsTable record, so it is renamed there too.
    if key.status in (KeyStatus.ACTIVE, *DISABLED_STATUSES):
        with _backend(ctx, key.source) as backend:
            backend.rename(key)
    key.save(update_fields=["name"])


@operations.register(drift.RECONCILE, _("Compare keys with the server"))
def reconcile(ctx: OperationContext) -> None:
    instance = instances.get(ctx.params["source"])
    kind = _wg_kind(instance)
    with instance.connect() as host, ctx.step(_("Read the protocol configuration")):
        registry = read_registry(host, kind)
    with ctx.step(_("Compare with the panel")):
        found = drift.reconcile(instance, registry)
        ctx.log(_("Differences: %(count)d.") % {"count": len(found)})


@operations.register(xray_profiles.BUILD_PROFILES, _("Prepare imported XRay keys for showing"))
def build_xray_profiles(ctx: OperationContext) -> None:
    """The profiles of imported XRay keys from server.json; keys that have one are left alone."""
    instance = instances.get(ctx.params["source"])
    with instance.connect() as host, ctx.step(_("Read the protocol configuration")):
        built = xray_profiles.XrayProfiles(instance, host).fill()
    ctx.log(_("Keys ready to be shown: %(count)d.") % {"count": built})


@operations.register(drift.RESOLVE, _("Resolve a difference with the server"))
def resolve_drift(ctx: OperationContext) -> None:
    """Re-check the difference first: the server or the panel may have changed since."""
    source, public_key = ctx.params["source"], ctx.params["public_key"]
    action = drift.DriftAction(ctx.params["action"])
    instance = instances.get(source)
    kind = _wg_kind(instance)
    with instance.connect() as host:
        with ctx.step(_("Read the protocol configuration")):
            registry = read_registry(host, kind)
            key = AccessKey.objects.filter(source=source, public_key=public_key).first()
            current = drift.finding_for(registry, public_key, key)
            if current is None:
                ctx.log(str(_("The difference is already gone.")))
            elif action not in drift.ALLOWED_ACTIONS[current.kind]:
                raise PeerError(PeerErrorCode.DRIFT_CHANGED, f"now {current.kind}")
        if current is not None:
            peers = WgKeys(instance, kind, PeerManager(host, ctx, kind))
            _resolve(peers, registry, source, current, action)
    Drift.objects.filter(source=source, public_key=public_key).delete()


def _wg_kind(instance: Instance) -> WgContainer:
    """Differences are found for WireGuard-family containers only."""
    if not isinstance(instance.container, WgContainer):
        raise PeerError(PeerErrorCode.UNSUPPORTED_SETUP, f"no drift for {instance.container.name}")
    return instance.container


def _resolve(
    peers: WgKeys,
    registry: AwgPeerRegistry,
    source: str,
    found: drift.Finding,
    action: drift.DriftAction,
) -> None:
    key = found.key
    if key is None:  # a peer the panel does not know
        if action == drift.DriftAction.ACCEPT:
            peer = registry.config.find_peer(found.public_key)
            assert peer is not None
            adopt_peer(source, peer, registry.clients, str(_ADDED_IN_APP))
        else:
            peers.peers.revoke(found.public_key)
    elif found.kind == DriftKind.MISSING:
        if action == drift.DriftAction.RESTORE:
            peers.enable(key)
        else:
            _forget(key)
    elif action == drift.DriftAction.REMOVE:
        if key.is_gone:
            peers.revoke(key)
        else:
            peers.disable(key)
    else:
        _set_status(key, KeyStatus.ACTIVE)
