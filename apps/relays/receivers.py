"""A relay sync right after a change of what the relays forward, not at the next minute.

The members of a group, the group port or relay, the service peer, the address of a member
server, or the port, keys and parameters of a member: a change of any of them queues a sync once
the transaction commits. The sync compares digests, so a change that is not on a relay costs
nothing; the sync by the minute stays for what no signal sees, such as DNS.
"""

from collections.abc import Iterable
from typing import Any

from django.db import transaction
from django.db.models import Model
from django.db.models.signals import post_delete, post_save

from apps.groups.models import GroupMember, ServerGroup
from apps.protocols.models import ProtocolInstance
from apps.relays.models import Relay, ServicePeer
from apps.servers.models import Server

# Fields of a member server and its instance that reach the config of a relay.
_SERVER_FIELDS = frozenset({"host"})
_INSTANCE_FIELDS = frozenset({"port", "public_key", "preshared_key", "params"})


def request_sync() -> None:
    """Queue a sync of the relays after the current transaction; nothing without a relay."""
    if Relay.objects.filter(installed_at__isnull=False).exists():
        transaction.on_commit(_send)


def _send() -> None:
    from apps.relays.tasks import sync  # noqa: PLC0415  the tasks module imports the services

    sync.delay()


def _touches(update_fields: Iterable[str] | None, fields: frozenset[str]) -> bool:
    return update_fields is None or not fields.isdisjoint(update_fields)


def on_group_changed(sender: type[Model], **kwargs: Any) -> None:
    request_sync()


def on_server_saved(
    sender: type[Server], instance: Server, update_fields: Iterable[str] | None, **kwargs: Any
) -> None:
    relayed = GroupMember.objects.filter(instance__server=instance, group__relay__isnull=False)
    if _touches(update_fields, _SERVER_FIELDS) and relayed.exists():
        request_sync()


def on_instance_saved(
    sender: type[ProtocolInstance],
    instance: ProtocolInstance,
    update_fields: Iterable[str] | None,
    **kwargs: Any,
) -> None:
    relayed = GroupMember.objects.filter(instance=instance, group__relay__isnull=False)
    if _touches(update_fields, _INSTANCE_FIELDS) and relayed.exists():
        request_sync()


def connect() -> None:
    for model in (GroupMember, ServerGroup, ServicePeer):
        uid = f"relays.sync.{model.__name__}"
        post_save.connect(on_group_changed, sender=model, dispatch_uid=f"{uid}.save")
        post_delete.connect(on_group_changed, sender=model, dispatch_uid=f"{uid}.delete")
    post_save.connect(on_server_saved, sender=Server, dispatch_uid="relays.sync.Server")
    post_save.connect(
        on_instance_saved, sender=ProtocolInstance, dispatch_uid="relays.sync.ProtocolInstance"
    )
