"""The clone peers of a split group: the active keys of the group, as its members have them.

The panel writes them to the relay as a file of their own (docs/SPLIT.md 4.7), so issuing,
revoking or editing a key rewrites that file and not the agent config. They come from the
panel's keys and not from awg0.conf of the leader: a sync then needs no SSH to a member.
"""

import hashlib
import json
from collections.abc import Mapping

from amnezia_core.awg import Peer
from amnezia_core.relay import ClonePeers
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.profile import ServerProfile
from apps.protocols.models import ProtocolInstance


def clone_peers(leader: ProtocolInstance) -> ClonePeers:
    """The peers of the active keys of the group the instance leads, oldest first."""
    keys = (
        AccessKey.objects.filter(source=leader.source, status=KeyStatus.ACTIVE)
        .exclude(client_ip=None)
        .order_by("pk")
    )
    return ClonePeers(tuple(_peer(key, leader.preshared_key) for key in keys))


def peers_digest(files: Mapping[str, str]) -> str:
    """The digest of the files of the split groups of a relay, by file name: it changes with any
    key peer, network or own domain."""
    return hashlib.sha256(json.dumps(sorted(files.items())).encode()).hexdigest()


def _peer(key: AccessKey, group_preshared_key: str) -> Peer:
    """The peer as WgKeys puts it on a member: the preshared key of its profile, if it has one."""
    preshared_key = (
        ServerProfile.from_json(key.profile).preshared_key if key.profile else group_preshared_key
    )
    return Peer(key.public_key, (f"{key.client_ip}/32",), preshared_key or None)
