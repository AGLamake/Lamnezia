"""A relay sync right after a change of what the relays forward, not at the next minute.

The members of a group, the group port or relay, the service peer, the address of a member
server, or the port, keys and parameters of a member: a change of any of them names the relays
of the groups it touches. A transaction queues one sync for all the relays it named, once it
commits, and the sync updates only them. The sync compares digests, so a change that is not on a
relay costs nothing; the sync by the minute stays for what no signal sees, such as DNS.
"""

from collections.abc import Iterable
from typing import Any

from django.db import transaction
from django.db.models import Model, QuerySet
from django.db.models.signals import post_delete, post_save, pre_save

from apps.groups.models import GroupMember, ServerGroup
from apps.protocols.models import ProtocolInstance
from apps.relays.models import Relay, ServicePeer
from apps.servers.models import Server

# Fields of a member server and its instance that reach the config of a relay.
_SERVER_FIELDS = frozenset({"host"})
_INSTANCE_FIELDS = frozenset({"port", "public_key", "preshared_key", "params"})


class SyncBatch:
    """The relay servers one transaction changed; on commit it sends one sync for all of them.

    The batch waits among the commit callbacks of the transaction, so a rolled back transaction
    or savepoint drops it with its other callbacks.
    """

    def __init__(self) -> None:
        self.servers: set[int] = set()
        self.sent = False

    def __call__(self) -> None:
        from apps.relays.tasks import sync  # noqa: PLC0415  the tasks module imports the services

        self.sent = True
        sync.delay(sorted(self.servers))

    @classmethod
    def add(cls, servers: set[int]) -> None:
        """Add the servers to the batch of the current transaction, queueing one if it has none."""
        connection = transaction.get_connection()
        waiting = (func for _, func, _ in connection.run_on_commit if isinstance(func, cls))
        batch = next((func for func in waiting if not func.sent), None)
        if batch is not None:
            batch.servers |= servers
            return
        batch = cls()
        batch.servers |= servers
        transaction.on_commit(batch)


def request_sync(relays: Iterable[int | None]) -> None:
    """Queue a sync of the installed relays among these servers after the current transaction."""
    named = {pk for pk in relays if pk is not None}
    installed = Relay.objects.filter(server_id__in=named, installed_at__isnull=False)
    servers = set(installed.values_list("server_id", flat=True))
    if servers:
        SyncBatch.add(servers)


def _relays_of(groups: QuerySet[ServerGroup]) -> list[int | None]:
    return list(groups.values_list("relay_id", flat=True))


def _touches(update_fields: Iterable[str] | None, fields: frozenset[str]) -> bool:
    return update_fields is None or not fields.isdisjoint(update_fields)


def on_group_saving(
    sender: type[ServerGroup],
    instance: ServerGroup,
    update_fields: Iterable[str] | None,
    **kwargs: Any,
) -> None:
    """A group moving to another relay leaves the old one: that relay is synced too."""
    if instance.pk and _touches(update_fields, frozenset({"relay"})):
        request_sync(_relays_of(ServerGroup.objects.filter(pk=instance.pk)))


def on_group_changed(sender: type[ServerGroup], instance: ServerGroup, **kwargs: Any) -> None:
    request_sync([instance.relay_id])


def on_group_part_changed(
    sender: type[Model], instance: GroupMember | ServicePeer, **kwargs: Any
) -> None:
    request_sync(_relays_of(ServerGroup.objects.filter(pk=instance.group_id)))


def on_server_saved(
    sender: type[Server], instance: Server, update_fields: Iterable[str] | None, **kwargs: Any
) -> None:
    if _touches(update_fields, _SERVER_FIELDS):
        request_sync(_relays_of(ServerGroup.objects.filter(members__instance__server=instance)))


def on_instance_saved(
    sender: type[ProtocolInstance],
    instance: ProtocolInstance,
    update_fields: Iterable[str] | None,
    **kwargs: Any,
) -> None:
    if _touches(update_fields, _INSTANCE_FIELDS):
        request_sync(_relays_of(ServerGroup.objects.filter(members__instance=instance)))


def connect() -> None:
    uid = "relays.sync.ServerGroup"
    pre_save.connect(on_group_saving, sender=ServerGroup, dispatch_uid=f"{uid}.saving")
    post_save.connect(on_group_changed, sender=ServerGroup, dispatch_uid=f"{uid}.save")
    post_delete.connect(on_group_changed, sender=ServerGroup, dispatch_uid=f"{uid}.delete")
    for model in (GroupMember, ServicePeer):
        uid = f"relays.sync.{model.__name__}"
        post_save.connect(on_group_part_changed, sender=model, dispatch_uid=f"{uid}.save")
        post_delete.connect(on_group_part_changed, sender=model, dispatch_uid=f"{uid}.delete")
    post_save.connect(on_server_saved, sender=Server, dispatch_uid="relays.sync.Server")
    post_save.connect(
        on_instance_saved, sender=ProtocolInstance, dispatch_uid="relays.sync.ProtocolInstance"
    )
