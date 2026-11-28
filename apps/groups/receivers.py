"""Group work on events of other apps: a fresh copy of every member after a key change, and
the counters of new keys on the members."""

from collections.abc import Sequence
from typing import Any

from django.utils import timezone

from apps.backups.services.freshness import request_fresh_copy
from apps.groups.services.addressing import key_sources
from apps.groups.services.replication import replica_instances
from apps.keys.signals import clients_adding, keys_written
from apps.traffic.services.recorder import TrafficRecorder


def on_keys_written(sender: object, source: str, **kwargs: Any) -> None:
    """Each member the change reached gets a copy, as the leading server does from backups."""
    for instance_id in replica_instances(source):
        request_fresh_copy(instance_id)


def on_clients_adding(
    sender: object, source: str, public_keys: Sequence[str], **kwargs: Any
) -> None:
    """New keys of a leading server count from their first byte on its members too."""
    members = [member for member, leader in key_sources().items() if leader == source]
    recorder, at = TrafficRecorder(), timezone.now()
    for member in members:
        for public_key in public_keys:
            recorder.expect_member_peer(member, public_key, at, source)


def connect() -> None:
    keys_written.connect(on_keys_written, dispatch_uid="groups.keys_written")
    clients_adding.connect(on_clients_adding, dispatch_uid="groups.traffic.clients_adding")
