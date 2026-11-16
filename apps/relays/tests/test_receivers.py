"""One relay sync per transaction, for the relays of the groups it changed."""

import pytest
from django.db import transaction
from django.utils import timezone

from apps.groups.models import GroupMember, ServerGroup
from apps.operations.models import Operation
from apps.protocols.tests.awg_case import OnCommit
from apps.relays.models import Relay
from apps.relays.receivers import SyncBatch
from apps.relays.services.deployment import UPDATE_KIND, sync_relays
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory

pytestmark = pytest.mark.django_db


def installed(server: Server) -> Relay:
    return Relay.objects.create(server=server, installed_at=timezone.now())


def batches(callbacks: list[object]) -> list[SyncBatch]:
    return [callback for callback in callbacks if isinstance(callback, SyncBatch)]


def test_several_changes_of_a_group_queue_one_sync(
    relayed: ServerGroup, relay_server: Server, django_capture_on_commit_callbacks: OnCommit
) -> None:
    installed(relay_server)
    member = GroupMember.objects.get(leading=False)

    with django_capture_on_commit_callbacks() as callbacks:
        relayed.port = 51820
        relayed.save(update_fields=["port"])
        member.save()
        relayed.save()

    (batch,) = batches(callbacks)
    assert batch.servers == {relay_server.pk}


def test_only_the_relay_of_the_changed_group_is_synced(
    relayed: ServerGroup, relay_server: Server, django_capture_on_commit_callbacks: OnCommit
) -> None:
    installed(relay_server)
    other_relay: Server = ServerFactory()
    ServerGroup.objects.create(name="Other", port=40000, relay=other_relay)
    installed(other_relay)

    with django_capture_on_commit_callbacks() as callbacks:
        relayed.save()

    (batch,) = batches(callbacks)
    assert batch.servers == {relay_server.pk}


def test_a_group_moving_to_another_relay_syncs_both(
    relayed: ServerGroup, relay_server: Server, django_capture_on_commit_callbacks: OnCommit
) -> None:
    installed(relay_server)
    new = installed(ServerFactory())

    with django_capture_on_commit_callbacks() as callbacks:
        relayed.relay = new.server
        relayed.save(update_fields=["relay"])

    (batch,) = batches(callbacks)
    assert batch.servers == {relay_server.pk, new.server_id}


def test_a_rolled_back_change_queues_nothing(
    relayed: ServerGroup, relay_server: Server, django_capture_on_commit_callbacks: OnCommit
) -> None:
    installed(relay_server)

    with (
        django_capture_on_commit_callbacks() as callbacks,
        pytest.raises(RuntimeError),
        transaction.atomic(),
    ):
        relayed.save()
        raise RuntimeError

    assert not batches(callbacks)


def test_a_sync_for_some_servers_updates_only_their_relays() -> None:
    chosen = installed(ServerFactory())
    installed(ServerFactory())

    sync_relays([chosen.server_id])

    (update,) = Operation.objects.filter(kind=UPDATE_KIND)
    assert update.params["server_id"] == chosen.server_id
