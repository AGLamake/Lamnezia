"""Reconciliation: where the keys of the panel and the peers on a server differ.

The Amnezia app edits the same files, so a server can get peers the panel does not know, lose
peers of active keys, or keep peers of keys the panel turned off.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from django.db import transaction
from django.utils import timezone

from amnezia_core.awg import Peer
from amnezia_core.awg_peers import AwgPeerRegistry
from amnezia_core.containers import WgContainer
from amnezia_core.xray import XrayContainer
from apps.accounts.models import User
from apps.audit.services import journal as audit
from apps.keys.errors import PeerError
from apps.keys.models import DISABLED_STATUSES, AccessKey, Drift, DriftKind, KeyStatus
from apps.keys.services.instances import Instance, instances
from apps.keys.services.peers import read_registry
from apps.keys.services.xray_profiles import XrayProfiles
from apps.operations.models import Operation
from apps.operations.services.launcher import launch
from apps.ssh.errors import SshError

logger = logging.getLogger(__name__)

RECONCILE = "keys.reconcile"
RESOLVE = "keys.resolve_drift"

_OFF = DISABLED_STATUSES | {KeyStatus.REVOKED}


class DriftAction(StrEnum):
    ACCEPT = "accept"  # the panel takes the server state
    REMOVE = "remove"  # the peer is taken off the server
    RESTORE = "restore"  # the peer of the key is put back on the server


ALLOWED_ACTIONS: dict[str, tuple[DriftAction, ...]] = {
    DriftKind.UNKNOWN_PEER: (DriftAction.ACCEPT, DriftAction.REMOVE),
    DriftKind.MISSING: (DriftAction.RESTORE, DriftAction.ACCEPT),
    DriftKind.UNEXPECTED: (DriftAction.REMOVE, DriftAction.ACCEPT),
}


@dataclass(frozen=True)
class Finding:
    kind: DriftKind
    public_key: str
    name: str
    client_ip: str | None
    key: AccessKey | None = None


def compare(registry: AwgPeerRegistry, keys: Iterable[AccessKey]) -> list[Finding]:
    """Differences between the server files and the keys of the same instance.

    Keys being issued or revoked are left out: their operation adds or removes the peer.
    """
    peers = {peer.public_key: peer for peer in registry.config.peers}
    known = {
        key.public_key: key
        for key in keys
        if key.status not in (KeyStatus.ISSUING, KeyStatus.REVOKING)
    }
    findings = []
    for public_key, peer in peers.items():
        key = known.get(public_key)
        if key is None:
            name = registry.clients.name(public_key) if public_key in registry.clients else None
            findings.append(Finding(DriftKind.UNKNOWN_PEER, public_key, name or "", _ip(peer)))
        elif key.status in _OFF:
            findings.append(Finding(DriftKind.UNEXPECTED, public_key, key.name, key.client_ip, key))
    for public_key, key in known.items():
        if key.status == KeyStatus.ACTIVE and public_key not in peers:
            findings.append(Finding(DriftKind.MISSING, public_key, key.name, key.client_ip, key))
    return findings


def finding_for(
    registry: AwgPeerRegistry, public_key: str, key: AccessKey | None
) -> Finding | None:
    """The difference for one peer now, or None when the server and the panel agree."""
    found = compare(registry, [key] if key else [])
    return next((f for f in found if f.public_key == public_key), None)


@transaction.atomic
def record(source: str, findings: Iterable[Finding]) -> list[Drift]:
    """Replace the differences of the instance with the latest ones."""
    Drift.objects.filter(source=source).delete()
    now = timezone.now()
    return Drift.objects.bulk_create(
        Drift(
            source=source,
            public_key=f.public_key,
            kind=f.kind,
            key=f.key,
            name=f.name,
            client_ip=f.client_ip,
            detected_at=now,
        )
        for f in findings
    )


def reconcile(instance: Instance, registry: AwgPeerRegistry) -> list[Drift]:
    keys = AccessKey.objects.filter(source=instance.source)
    return record(instance.source, compare(registry, keys))


def reconcile_all() -> None:
    """Every instance in turn; a server that cannot be read does not stop the others.

    XRay keeps no per-client state to compare; its server.json is read for a new shortId.
    """
    for instance in instances:
        kind = instance.container
        if not isinstance(kind, WgContainer | XrayContainer):
            continue
        try:
            with instance.connect() as host:
                if isinstance(kind, XrayContainer):
                    XrayProfiles(instance, host).current()
                else:
                    reconcile(instance, read_registry(host, kind))
        except (SshError, PeerError) as error:
            logger.warning("reconciliation of %s failed: %s", instance.source, error.code)


class DriftActions:
    """Start reconciliations and resolutions; resolutions go to the audit journal."""

    def __init__(self, actor: User, ip: str | None = None) -> None:
        self._actor = actor
        self._ip = ip

    def check(self, source: str) -> Operation:
        instance = instances.get(source)
        return launch(RECONCILE, {"source": source}, user=self._actor, subject=instance.title)

    def resolve(self, drift: Drift, action: DriftAction) -> Operation:
        if action not in ALLOWED_ACTIONS[drift.kind]:
            raise ValueError(f"{action} does not apply to {drift.kind}")
        params = {"source": drift.source, "public_key": drift.public_key, "action": str(action)}
        operation = launch(RESOLVE, params, user=self._actor, subject=str(drift))
        audit.log(
            RESOLVE,
            actor=self._actor,
            target=drift.key,
            data={**params, "kind": drift.kind, "operation": operation.pk},
            ip=self._ip,
        )
        return operation


def _ip(peer: Peer) -> str | None:
    addresses = peer.addresses
    return str(addresses[0]) if addresses else None
