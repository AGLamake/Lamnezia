"""A fresh copy of a protocol or a service soon after it changes, so a restore has what is new.

The hourly copy misses the keys issued since; a key operation asks for one more copy. The
operations of a few seconds share it: it is taken once the first of them is a little old.
A new secret or new settings of a service ask for a copy at once: the saved copy holds the
old secret, which the panel no longer knows the service by.
"""

from django.core.cache import cache

from apps.backups.services.snapshot import save_instance_backup, save_service_copy

# The key operations right after the first one still get into the same copy.
FRESH_COPY_DELAY = 30
# A copy asked for and lost with its worker does not stop the next one for long.
_PENDING_TIMEOUT = 600


def _pending_key(kind: str, pk: int) -> str:
    return f"backups:fresh-copy:{kind}:{pk}"


def request_fresh_copy(instance_id: int) -> None:
    """Take a copy of the instance in `FRESH_COPY_DELAY` seconds, unless one is already due."""
    from apps.backups.tasks import save_protocol_backup  # noqa: PLC0415 - tasks import services

    if cache.add(_pending_key("instance", instance_id), True, timeout=_PENDING_TIMEOUT):
        save_protocol_backup.apply_async((instance_id,), countdown=FRESH_COPY_DELAY)


def request_fresh_service_copy(service_id: int) -> None:
    """Take a copy of the service now, unless one is already due."""
    from apps.backups.tasks import save_service_backup  # noqa: PLC0415 - tasks import services

    if cache.add(_pending_key("service", service_id), True, timeout=_PENDING_TIMEOUT):
        save_service_backup.apply_async((service_id,), countdown=0)


def take_fresh_copy(instance_id: int) -> None:
    """The copy asked for; a key change from now on asks for the next one.

    The flag goes before the files are read, so a change that is on the server before the
    read is in this copy and one after it gets a new copy.
    """
    cache.delete(_pending_key("instance", instance_id))
    save_instance_backup(instance_id)


def take_fresh_service_copy(service_id: int) -> None:
    """The copy of the service asked for; as for a protocol, the flag goes first."""
    cache.delete(_pending_key("service", service_id))
    save_service_copy(service_id)
