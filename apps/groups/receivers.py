"""Group work on events of other apps: a fresh copy of every member after a key change."""

from typing import Any

from apps.backups.services.freshness import request_fresh_copy
from apps.groups.services.replication import replica_instances
from apps.keys.signals import keys_written


def on_keys_written(sender: object, source: str, **kwargs: Any) -> None:
    """Each member the change reached gets a copy, as the leading server does from backups."""
    for instance_id in replica_instances(source):
        request_fresh_copy(instance_id)


def connect() -> None:
    keys_written.connect(on_keys_written, dispatch_uid="groups.keys_written")
