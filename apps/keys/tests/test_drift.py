from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Any

import pytest
from django.test import Client

from amnezia_core.awg import AWG_CLIENTS_TABLE_PATH, AWG_CONFIG_PATH, AwgServerConfig
from amnezia_core.clients_table import ClientsTable
from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.keys.models import AccessKey, Drift, DriftKind, KeyStatus
from apps.keys.services.drift import RESOLVE, DriftAction, DriftActions, reconcile_all
from apps.keys.services.instances import Instance, instances
from apps.keys.tests.conftest import HOST, SOURCE
from apps.keys.tests.fake_server import FakeAwgServer
from apps.operations.models import Operation, OperationStatus
from apps.ssh.errors import ErrorCode, SshConnectionError
from apps.ssh.services import RemoteHost

pytestmark = pytest.mark.django_db

OnCommit = Callable[..., AbstractContextManager[Any]]
ANNA = "jhkQdvDcgDYudvey3hItwkaQoDy/bLT6QnsyYHwCIlo="
APP_CLIENT = "Q2+/5jIlpVjLMKDArTagVYSPsI5fcqPNpSem9NMi50s="
GONE = "3Zx7Q2bBVZc/Zx3x3oI8yQ1u9bKgk1PZ4w7Gm3kq9Ew="


@pytest.fixture
def actions() -> DriftActions:
    return DriftActions(UserFactory(role=Role.ADMIN), "198.51.100.7")


@pytest.fixture
def run(
    django_capture_on_commit_callbacks: OnCommit,
) -> Callable[[Callable[[], Operation]], Operation]:
    def runner(start: Callable[[], Operation]) -> Operation:
        with django_capture_on_commit_callbacks(execute=True):
            operation = start()
        operation.refresh_from_db()
        return operation

    return runner


def key(public_key: str, status: KeyStatus = KeyStatus.ACTIVE, **fields: Any) -> AccessKey:
    defaults = {"name": public_key[:6], "client_ip": "10.8.1.1"}
    return AccessKey.objects.create(
        source=SOURCE, public_key=public_key, status=status, **{**defaults, **fields}
    )


def peers_of(server: FakeAwgServer) -> set[str]:
    return {p.public_key for p in AwgServerConfig.parse(server.files[AWG_CONFIG_PATH]).peers}


def drifts() -> dict[str, str]:
    return dict(Drift.objects.values_list("public_key", "kind"))


def check(actions: DriftActions, run: Callable[[Callable[[], Operation]], Operation]) -> None:
    assert run(lambda: actions.check(SOURCE)).status == OperationStatus.SUCCEEDED


def resolve(
    actions: DriftActions,
    run: Callable[[Callable[[], Operation]], Operation],
    public_key: str,
    action: DriftAction,
) -> Operation:
    return run(lambda: actions.resolve(Drift.objects.get(public_key=public_key), action))


def test_peer_added_in_the_app_is_reported(
    awg_server: FakeAwgServer, actions: DriftActions, run: Any
) -> None:
    key(ANNA)

    check(actions, run)

    drift = Drift.objects.get()
    assert (drift.public_key, drift.kind, drift.name) == (
        APP_CLIENT,
        DriftKind.UNKNOWN_PEER,
        "Client 1",
    )
    assert drift.key is None


def test_removed_and_unexpected_peers_are_reported(
    awg_server: FakeAwgServer, actions: DriftActions, run: Any
) -> None:
    key(ANNA, KeyStatus.DISABLED_MANUAL)
    key(APP_CLIENT, KeyStatus.REVOKED, client_ip="10.8.1.2")
    key(GONE, client_ip="10.8.1.3")
    key("issuing-key", KeyStatus.ISSUING)
    key("revoked-key", KeyStatus.REVOKED)

    check(actions, run)

    assert drifts() == {
        ANNA: DriftKind.UNEXPECTED,
        APP_CLIENT: DriftKind.UNEXPECTED,
        GONE: DriftKind.MISSING,
    }


def test_a_new_check_replaces_the_old_findings(
    awg_server: FakeAwgServer, actions: DriftActions, run: Any
) -> None:
    check(actions, run)
    key(ANNA)
    key(APP_CLIENT)

    check(actions, run)

    assert drifts() == {}


def test_accepting_an_app_peer_adds_an_ownerless_key(
    awg_server: FakeAwgServer, actions: DriftActions, run: Any
) -> None:
    key(ANNA)
    check(actions, run)
    files = dict(awg_server.files)

    resolve(actions, run, APP_CLIENT, DriftAction.ACCEPT)

    adopted = AccessKey.objects.get(public_key=APP_CLIENT)
    assert (adopted.name, adopted.client_ip, adopted.user) == ("Client 1", "10.8.1.2", None)
    assert adopted.status == KeyStatus.ACTIVE
    assert awg_server.files == files
    assert drifts() == {}


def test_removing_an_app_peer_takes_it_off_the_server(
    awg_server: FakeAwgServer, actions: DriftActions, run: Any
) -> None:
    key(ANNA)
    check(actions, run)

    resolve(actions, run, APP_CLIENT, DriftAction.REMOVE)

    assert peers_of(awg_server) == {ANNA}
    assert APP_CLIENT not in ClientsTable.parse(awg_server.files[AWG_CLIENTS_TABLE_PATH])
    assert not AccessKey.objects.filter(public_key=APP_CLIENT).exists()


def test_restoring_puts_the_peer_back_with_its_address(
    awg_server: FakeAwgServer, actions: DriftActions, run: Any
) -> None:
    key(ANNA)
    key(APP_CLIENT)
    restored = key(GONE, client_ip="10.8.1.3")
    check(actions, run)

    resolve(actions, run, GONE, DriftAction.RESTORE)

    peer = AwgServerConfig.parse(awg_server.files[AWG_CONFIG_PATH]).find_peer(GONE)
    assert peer is not None
    assert peer.allowed_ips == ("10.8.1.3/32",)
    restored.refresh_from_db()
    assert restored.status == KeyStatus.ACTIVE


def test_accepting_a_removed_peer_revokes_the_key(
    awg_server: FakeAwgServer, actions: DriftActions, run: Any
) -> None:
    key(ANNA)
    key(APP_CLIENT)
    gone = key(GONE, private_key="secret", profile="{}")
    check(actions, run)

    resolve(actions, run, GONE, DriftAction.ACCEPT)

    gone.refresh_from_db()
    assert gone.status == KeyStatus.REVOKED
    assert (gone.private_key, gone.profile) == ("", "")


def test_removing_a_disabled_key_peer_keeps_its_name(
    awg_server: FakeAwgServer, actions: DriftActions, run: Any
) -> None:
    disabled = key(ANNA, KeyStatus.DISABLED_MANUAL)
    key(APP_CLIENT)
    check(actions, run)

    resolve(actions, run, ANNA, DriftAction.REMOVE)

    assert peers_of(awg_server) == {APP_CLIENT}
    assert ANNA in ClientsTable.parse(awg_server.files[AWG_CLIENTS_TABLE_PATH])
    disabled.refresh_from_db()
    assert disabled.status == KeyStatus.DISABLED_MANUAL


def test_accepting_an_unexpected_peer_turns_the_key_on(
    awg_server: FakeAwgServer, actions: DriftActions, run: Any
) -> None:
    disabled = key(ANNA, KeyStatus.DISABLED_MANUAL)
    key(APP_CLIENT)
    check(actions, run)

    resolve(actions, run, ANNA, DriftAction.ACCEPT)

    disabled.refresh_from_db()
    assert disabled.status == KeyStatus.ACTIVE


def test_a_difference_resolved_meanwhile_changes_nothing(
    awg_server: FakeAwgServer, actions: DriftActions, run: Any
) -> None:
    key(ANNA)
    check(actions, run)
    key(APP_CLIENT)
    files = dict(awg_server.files)

    operation = resolve(actions, run, APP_CLIENT, DriftAction.REMOVE)

    assert operation.status == OperationStatus.SUCCEEDED
    assert awg_server.files == files
    assert drifts() == {}


def test_a_difference_that_changed_kind_is_refused(
    awg_server: FakeAwgServer, actions: DriftActions, run: Any
) -> None:
    key(ANNA)
    key(APP_CLIENT)
    gone = key(GONE)
    check(actions, run)
    gone.status = KeyStatus.DISABLED_MANUAL
    gone.save()
    awg_server.files[AWG_CONFIG_PATH] += f"\n[Peer]\nPublicKey = {GONE}\nAllowedIPs = 10.8.1.3/32\n"

    operation = resolve(actions, run, GONE, DriftAction.RESTORE)

    assert operation.status == OperationStatus.FAILED
    assert (
        operation.error_message == "The difference with the server has changed since it was found."
    )


def test_resolution_is_written_to_the_audit_journal(
    awg_server: FakeAwgServer, actions: DriftActions, run: Any
) -> None:
    key(ANNA)
    check(actions, run)

    resolve(actions, run, APP_CLIENT, DriftAction.ACCEPT)

    event = AuditEvent.objects.get(action=RESOLVE)
    assert event.data["kind"] == DriftKind.UNKNOWN_PEER
    assert event.data["action"] == "accept"
    assert event.ip == "198.51.100.7"


def test_periodic_check_goes_on_when_a_server_is_down(awg_server: FakeAwgServer) -> None:
    key(ANNA)

    def refuse() -> Iterator[RemoteHost]:
        raise SshConnectionError(ErrorCode.HOST_UNREACHABLE)

    def down() -> list[Instance]:
        return [Instance("awg:down", "Down", HOST, refuse)]  # type: ignore[arg-type]

    instances.register(down)
    try:
        reconcile_all()
    finally:
        instances.unregister(down)

    assert drifts() == {APP_CLIENT: DriftKind.UNKNOWN_PEER}


@pytest.fixture
def staff_client(client: Client) -> Client:
    client.force_login(UserFactory(role=Role.ADMIN))
    return client


def test_page_offers_the_actions_of_each_difference(
    awg_server: FakeAwgServer, actions: DriftActions, run: Any, staff_client: Client
) -> None:
    key(ANNA)
    check(actions, run)

    html = staff_client.get("/keys/drift/").content.decode()

    assert "Amsterdam" in html
    assert "Client 1" in html
    assert "Add to the panel" in html
    assert "Remove from the server" in html
    assert "Restore on the server" not in html


def test_matching_sources_share_one_short_list(
    awg_server: FakeAwgServer, staff_client: Client
) -> None:
    html = staff_client.get("/keys/drift/").content.decode()

    assert "Every server matches the panel" in html
    assert "Check Amsterdam" in html


def test_an_action_that_does_not_fit_the_difference_is_refused(
    awg_server: FakeAwgServer, actions: DriftActions, run: Any, staff_client: Client
) -> None:
    key(ANNA)
    check(actions, run)
    drift = Drift.objects.get()

    response = staff_client.post(f"/keys/drift/{drift.pk}/restore/")

    assert response.status_code == 404


def test_check_button_starts_a_check(awg_server: FakeAwgServer, staff_client: Client) -> None:
    response = staff_client.post("/keys/drift/check/", {"source": SOURCE})

    assert response.status_code == 302
    assert Operation.objects.get().params == {"source": SOURCE}
