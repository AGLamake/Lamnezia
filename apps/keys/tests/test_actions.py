import pytest
from django.db import connection
from django.utils import timezone

from amnezia_core.awg import AWG_CLIENTS_TABLE_PATH, AWG_CONFIG_PATH, AwgServerConfig
from amnezia_core.clients_table import LAMNEZIA_USER_ID, ClientsTable
from amnezia_core.vpn_link import decode_vpn_link
from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.keys import operations as key_operations
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services.actions import REVOKE, KeyActions, KeyRevokingError
from apps.keys.services.instances import UnknownInstanceError
from apps.keys.services.profile import KeyExport
from apps.keys.services.reissue import REISSUE
from apps.keys.signals import clients_adding, keys_written
from apps.keys.tests.conftest import HOST, SOURCE, OnCommit, Run
from apps.keys.tests.fake_server import FakeAwgServer
from apps.operations.models import Operation
from apps.operations.services.context import OperationContext

pytestmark = pytest.mark.django_db


def peers_of(server: FakeAwgServer) -> set[str]:
    return {p.public_key for p in AwgServerConfig.parse(server.files[AWG_CONFIG_PATH]).peers}


def issued(actions: KeyActions, run: Run, owner: User | None = None) -> AccessKey:
    run(lambda: actions.issue(SOURCE, "iPhone", owner))
    return AccessKey.objects.latest("pk")


def test_issue_adds_the_peer_and_keeps_a_profile(
    awg_server: FakeAwgServer, actions: KeyActions, run: Run
) -> None:
    owner = UserFactory()
    key = issued(actions, run, owner)

    assert key.status == KeyStatus.ACTIVE
    assert key.public_key in peers_of(awg_server)
    clients = ClientsTable.parse(awg_server.files[AWG_CLIENTS_TABLE_PATH])
    assert clients.user_data(key.public_key)[LAMNEZIA_USER_ID] == owner.pk
    assert key.client_ip == "10.8.1.3"


def test_private_key_is_stored_encrypted(
    awg_server: FakeAwgServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)

    with connection.cursor() as cursor:
        cursor.execute("SELECT private_key, profile FROM keys_accesskey WHERE id = %s", [key.pk])
        stored_key, stored_profile = cursor.fetchone()
    assert key.private_key and key.private_key not in stored_key
    assert HOST not in stored_profile


def test_issued_key_exports_for_the_amnezia_app(
    awg_server: FakeAwgServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)

    export = KeyExport.of(key)
    config = decode_vpn_link(export.vpn_link)

    assert config["hostName"] == HOST
    assert config["defaultContainer"] == "amnezia-awg2"
    assert key.public_key in config["containers"][0]["awg"]["last_config"]
    assert f"PrivateKey = {key.private_key}" in _content(export, "conf")
    assert export.qr_codes()[0].startswith("<svg")


def test_issue_is_idempotent(awg_server: FakeAwgServer, actions: KeyActions, run: Run) -> None:
    key = issued(actions, run)
    before = awg_server.files[AWG_CONFIG_PATH]

    key_operations.issue(
        OperationContext(
            Operation.objects.create(kind="keys.issue", params={"key_id": key.pk}, language="en")
        )
    )

    assert awg_server.files[AWG_CONFIG_PATH] == before
    key.refresh_from_db()
    assert key.client_ip == "10.8.1.3"


def test_issued_key_is_announced_once_on_the_server(
    awg_server: FakeAwgServer, actions: KeyActions, run: Run
) -> None:
    sources: list[str] = []

    def receiver(sender: object, source: str, **kwargs: object) -> None:
        sources.append(source)

    keys_written.connect(receiver)
    try:
        key = issued(actions, run)
    finally:
        keys_written.disconnect(receiver)

    assert key.public_key in peers_of(awg_server)
    assert sources == [SOURCE]


def test_new_key_is_announced_before_it_reaches_the_server(
    awg_server: FakeAwgServer, actions: KeyActions, run: Run
) -> None:
    """Traffic counts the key from zero only if it knows of it before the first reading."""
    calls: list[tuple[str, list[str], bool]] = []

    def receiver(sender: object, source: str, public_keys: list[str], **kwargs: object) -> None:
        calls.append((source, public_keys, bool(set(public_keys) & peers_of(awg_server))))

    clients_adding.connect(receiver)
    try:
        key = issued(actions, run)
    finally:
        clients_adding.disconnect(receiver)

    assert calls == [(SOURCE, [key.public_key], False)]


def test_issue_on_unknown_instance_is_refused(actions: KeyActions) -> None:
    with pytest.raises(UnknownInstanceError):
        actions.issue("awg:404", "iPhone", None)
    assert not AccessKey.objects.exists()


def test_disable_and_enable_keep_the_address(
    awg_server: FakeAwgServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)

    run(lambda: actions.disable(key))
    key.refresh_from_db()
    assert key.status == KeyStatus.DISABLED_MANUAL
    assert key.public_key not in peers_of(awg_server)

    other = issued(actions, run)
    assert other.client_ip != key.client_ip

    run(lambda: actions.enable(key))
    key.refresh_from_db()
    assert key.status == KeyStatus.ACTIVE
    assert key.public_key in peers_of(awg_server)
    assert key.client_ip == "10.8.1.3"


def test_issue_and_revoke_are_in_the_audit_journal(
    awg_server: FakeAwgServer, actions: KeyActions, run: Run
) -> None:
    owner = UserFactory()
    key = issued(actions, run, owner)
    run(lambda: actions.revoke(key))

    events = list(AuditEvent.objects.order_by("pk"))
    assert [e.action for e in events] == ["keys.issue", "keys.revoke"]
    assert all(e.target_id == str(key.pk) and e.actor_name for e in events)
    assert events[0].data["owner_id"] == owner.pk
    assert key.private_key not in str(events[0].data)


def test_revoke_removes_the_peer_and_forgets_secrets(
    awg_server: FakeAwgServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)

    run(lambda: actions.revoke(key))

    key.refresh_from_db()
    assert key.status == KeyStatus.REVOKED
    assert key.revoked_at is not None
    assert (key.private_key, key.profile) == ("", "")
    assert not key.can_export
    assert key.public_key not in peers_of(awg_server)


def test_assign_gives_an_orphan_key_to_a_user(
    awg_server: FakeAwgServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)
    owner = UserFactory()

    run(lambda: actions.assign(key, owner))

    key.refresh_from_db()
    assert key.user == owner
    clients = ClientsTable.parse(awg_server.files[AWG_CLIENTS_TABLE_PATH])
    assert clients.user_data(key.public_key)[LAMNEZIA_USER_ID] == owner.pk


def test_reset_first_view(actions: KeyActions) -> None:
    key = AccessKey.objects.create(
        source=SOURCE, name="old", public_key="pk", first_viewed_at=timezone.now()
    )

    actions.reset_first_view(key)

    key.refresh_from_db()
    assert key.first_viewed_at is None
    assert AuditEvent.objects.get().action == "keys.reset_view"


def test_rename_changes_the_name_on_the_server(
    awg_server: FakeAwgServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)

    run(lambda: actions.rename(key, "Anna's phone"))

    key.refresh_from_db()
    assert key.name == "Anna's phone"
    clients = ClientsTable.parse(awg_server.files[AWG_CLIENTS_TABLE_PATH])
    assert clients.name(key.public_key) == "Anna's phone"


def test_reissue_revokes_the_old_key_and_issues_a_new_one(
    awg_server: FakeAwgServer,
    actions: KeyActions,
    run: Run,
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    owner = UserFactory()
    old = issued(actions, run, owner)
    before = Operation.objects.count()

    with django_capture_on_commit_callbacks(execute=True):
        new = actions.reissue(old)

    new.refresh_from_db()
    old.refresh_from_db()
    assert Operation.objects.count() == before + 1
    assert Operation.objects.latest("pk").kind == REISSUE
    assert old.status == KeyStatus.REVOKED
    assert (new.status, new.name, new.user) == (KeyStatus.ACTIVE, "iPhone", owner)
    assert new.public_key in peers_of(awg_server)
    assert old.public_key not in peers_of(awg_server)


def test_revoke_marks_the_key_revoking_and_a_second_one_starts_nothing(
    awg_server: FakeAwgServer, actions: KeyActions, run: Run
) -> None:
    key = issued(actions, run)

    actions.revoke(key)

    key.refresh_from_db()
    assert key.status == KeyStatus.REVOKING
    with pytest.raises(KeyRevokingError):
        actions.revoke(key)
    with pytest.raises(KeyRevokingError):
        actions.reissue(key)
    assert actions.change_state(key, "revoke") is None
    assert Operation.objects.filter(kind=REVOKE).count() == 1


def _content(export: KeyExport, extension: str) -> str:
    file = export.file(extension)
    return file.content if file is not None else ""


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ("disable", KeyStatus.DISABLED_MANUAL),
        ("enable", KeyStatus.ACTIVE),
        ("revoke", KeyStatus.REVOKED),
        ("explode", KeyStatus.ACTIVE),
    ],
)
def test_state_change_not_allowed_starts_nothing(
    actions: KeyActions, change: str, status: str
) -> None:
    key = AccessKey.objects.create(source=SOURCE, name="iPhone", public_key="pub", status=status)

    assert actions.change_state(key, change) is None
    assert not Operation.objects.exists()


def test_allowed_state_change_starts_the_operation(actions: KeyActions) -> None:
    key = AccessKey.objects.create(
        source=SOURCE, name="iPhone", public_key="pub", status=KeyStatus.ACTIVE
    )

    operation = actions.change_state(key, "disable")

    assert operation is not None
    assert operation.kind == "keys.disable"
