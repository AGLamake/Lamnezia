"""A relay operation that found a server busy goes again once the operation that held it is over."""

from datetime import timedelta

import pytest
from django.db import transaction
from django.utils import timezone
from pytest import MonkeyPatch

from apps.accounts.models import User
from apps.groups.models import ServerGroup
from apps.groups.services.settings import GroupSettingsChange, start_group_settings
from apps.groups.tests.conftest import succeeded
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.runner import OperationRunner
from apps.protocols.models import ProtocolInstance
from apps.protocols.services.awg_settings import AwgSettings
from apps.protocols.tests.awg_case import OnCommit, Start
from apps.relays.models import Relay
from apps.relays.receivers import SyncBatch
from apps.relays.services.config import RelayPlan
from apps.relays.services.deployment import (
    INSTALL_KIND,
    PEERS_KIND,
    UPDATE_KIND,
    blocked_by,
)
from apps.relays.tests.test_deployment import busy_first, run
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.errors import ErrorCode

pytestmark = pytest.mark.django_db

OTHER_KIND = "groups.settings"
NEW_PORT = 40000


def running(kind: str = OTHER_KIND) -> Operation:
    return Operation.objects.create(
        kind=kind, status=OperationStatus.RUNNING, started_at=timezone.now()
    )


def failed_busy(server: Server, kind: str = UPDATE_KIND, *, ago: float = 0) -> Operation:
    now = timezone.now() - timedelta(seconds=ago)
    return Operation.objects.create(
        kind=kind,
        params={"server_id": server.pk},
        status=OperationStatus.FAILED,
        error_code=ErrorCode.SERVER_BUSY,
        started_at=now,
        finished_at=now,
    )


def installed() -> Server:
    server: Server = ServerFactory()
    Relay.objects.create(server=server, installed_at=timezone.now())
    return server


def synced_on_finish(operation: Operation, on_commit: OnCommit) -> list[set[int]]:
    """The servers of each relay sync the end of `operation` queued."""
    with on_commit() as callbacks:
        operation.finish(OperationStatus.SUCCEEDED)
    return [callback.servers for callback in callbacks if isinstance(callback, SyncBatch)]


def send_syncs_now() -> None:
    """Send the queued relay syncs at once, as a worker's save outside a transaction does; the
    transaction of a test would send them only after the operation."""
    waiting = transaction.get_connection().run_on_commit
    for entry in [entry for entry in waiting if isinstance(entry[1], SyncBatch)]:
        waiting.remove(entry)
        entry[1]()


def updates() -> list[Operation]:
    return list(Operation.objects.filter(kind=UPDATE_KIND).order_by("pk"))


@pytest.mark.usefixtures("relayed")
def test_a_group_settings_change_and_the_relay_update_both_reach_the_end(
    relay_server: Server,
    leader: ProtocolInstance,
    admin: User,
    start: Start,
    monkeypatch: MonkeyPatch,
) -> None:
    """The change saves the new parameters and the relay update starts at once, while the change
    still holds the leading server: it finds it busy, and goes again when the change is over."""
    succeeded(run(INSTALL_KIND, relay_server, admin, start))
    busy_first(monkeypatch, "enable")
    save = GroupSettingsChange._save

    def save_while_holding(self: GroupSettingsChange, *args: object, **kwargs: object) -> object:
        keys = save(self, *args, **kwargs)  # type: ignore[arg-type]
        send_syncs_now()
        (update,) = updates()
        OperationRunner().run(update.pk)
        return keys

    monkeypatch.setattr(GroupSettingsChange, "_save", save_while_holding)
    current = AwgSettings.of(leader)
    settings = AwgSettings(
        current.port, {**current.params, "Jc": "7"}, current.dns, current.logs, current.subnet
    )

    group = ServerGroup.objects.get()
    succeeded(start(lambda: start_group_settings(group, settings, user=admin, reissue=False)))

    first, second = updates()
    assert (first.status, first.error_code) == (OperationStatus.FAILED, ErrorCode.SERVER_BUSY)
    succeeded(second)
    relay = Relay.objects.get(server=relay_server)
    plan = RelayPlan.of(relay_server)
    assert relay.synced == plan.digest(plan.config())


def test_the_end_of_an_operation_that_ran_meanwhile_syncs_the_relay(
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    server = installed()
    other = running()
    failed_busy(server)

    assert synced_on_finish(other, django_capture_on_commit_callbacks) == [{server.pk}]


def test_one_finished_operation_queues_one_sync_for_every_relay_it_held(
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    first, second = installed(), installed()
    other = running()
    failed_busy(first)
    failed_busy(second, PEERS_KIND)

    assert synced_on_finish(other, django_capture_on_commit_callbacks) == [{first.pk, second.pk}]


def test_an_operation_started_after_the_failure_is_not_its_cause(
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    server = installed()
    failed_busy(server, ago=1)
    later = running()

    assert synced_on_finish(later, django_capture_on_commit_callbacks) == []


def test_a_relay_operation_does_not_retry_after_itself_or_another_relay_operation(
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    """An update that finds the server busy again waits for an operation of another kind."""
    server = installed()
    update = running(UPDATE_KIND)
    failed_busy(server)

    assert synced_on_finish(update, django_capture_on_commit_callbacks) == []


def test_a_relay_tried_again_meanwhile_is_not_tried_twice(
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    """A later relay operation, waiting, done or failed otherwise, answers for the busy one."""
    server = installed()
    other = running()
    failed_busy(server)
    Operation.objects.create(kind=UPDATE_KIND, params={"server_id": server.pk})

    assert synced_on_finish(other, django_capture_on_commit_callbacks) == []


def test_a_busy_relay_that_is_not_installed_is_left_alone(
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    """An install found a server busy: the sync does not install, so nothing is queued."""
    server: Server = ServerFactory()
    Relay.objects.create(server=server)
    other = running()
    failed_busy(server, INSTALL_KIND)

    assert blocked_by(other) == {server.pk}
    assert synced_on_finish(other, django_capture_on_commit_callbacks) == []


def test_two_operations_over_retry_the_update_once(  # noqa: PLR0913, PLR0917
    relayed: ServerGroup,
    relay_server: Server,
    admin: User,
    start: Start,
    monkeypatch: MonkeyPatch,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    succeeded(run(INSTALL_KIND, relay_server, admin, start))
    first, second = running(), running()
    busy_first(monkeypatch, "enable")
    with django_capture_on_commit_callbacks(execute=True):
        relayed.port = NEW_PORT
        relayed.save(update_fields=["port"])

    for other in (first, second):
        with django_capture_on_commit_callbacks(execute=True):
            other.finish(OperationStatus.SUCCEEDED)

    assert [update.status for update in updates()] == [
        OperationStatus.FAILED,
        OperationStatus.SUCCEEDED,
    ]
