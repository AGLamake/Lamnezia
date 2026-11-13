"""Keys reissued in a batch: one operation per instance, one change of the server files."""

from collections.abc import Callable, Iterator
from contextlib import nullcontext
from functools import partial

import pytest

from amnezia_core.awg import AWG_CONFIG_PATH, AwgServerConfig
from amnezia_core.xray import (
    XRAY,
    XRAY_CONFIG_PATH,
    XRAY_OWN_CLIENT_PATH,
    XRAY_PUBLIC_KEY_PATH,
    XrayServerConfig,
    client_fingerprint,
)
from apps.accounts.models import Role, Status, User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.keys.errors import PeerErrorCode
from apps.keys.models import AccessKey, KeyStatus
from apps.keys.services import reissue
from apps.keys.services.actions import KeyActions
from apps.keys.services.instances import Instance, instances
from apps.keys.services.reissue import REISSUE, KeyReissue, ReissueStart
from apps.keys.tests.conftest import SOURCE, OnCommit, Run
from apps.keys.tests.fake_server import FakeAwgServer
from apps.keys.tests.fake_xray import FakeXrayServer
from apps.keys.tests.steps import FIXTURES
from apps.operations.models import Operation, OperationStatus

pytestmark = pytest.mark.django_db

XRAY_SOURCE = "xray:1"
OWN_CLIENT = "6f0c3e52-1b7a-4c8e-9d2f-0a1b2c3d4e5f"

Start = Callable[[Callable[[], ReissueStart]], ReissueStart]


@pytest.fixture
def xray_server() -> Iterator[FakeXrayServer]:
    server = FakeXrayServer(
        {
            XRAY_CONFIG_PATH: (FIXTURES / "xray_server.json").read_text(encoding="utf-8"),
            XRAY_OWN_CLIENT_PATH: f"{OWN_CLIENT}\n",
            XRAY_PUBLIC_KEY_PATH: "Wv1cT4hZbYk0n3m8dQeR2sF5gJ7aL9pX6uI0oE1yK2c\n",
        }
    )

    def provider() -> list[Instance]:
        return [
            Instance(
                XRAY_SOURCE, "Frankfurt", "203.0.113.20", lambda: nullcontext(server.host()), XRAY
            )
        ]

    instances.register(provider)
    yield server
    instances.unregister(provider)


@pytest.fixture
def start(django_capture_on_commit_callbacks: OnCommit) -> Start:
    """Start the batch and run its operations at once, as the worker would."""

    def runner(begin: Callable[[], ReissueStart]) -> ReissueStart:
        with django_capture_on_commit_callbacks(execute=True):
            started = begin()
        for operation in started.operations:
            operation.refresh_from_db()
        return started

    return runner


@pytest.fixture
def admin() -> User:
    return UserFactory(role=Role.ADMIN)


def issued(actions: KeyActions, run: Run, source: str, count: int) -> list[AccessKey]:
    for number in range(count):
        run(partial(actions.issue, source, f"phone {number}", UserFactory()))
    return list(AccessKey.objects.filter(source=source).order_by("pk"))


def awg_peers(server: FakeAwgServer) -> set[str]:
    config = AwgServerConfig.parse(server.files[AWG_CONFIG_PATH])
    return {peer.public_key for peer in config.peers}


def replacement(old: AccessKey) -> AccessKey:
    return AccessKey.objects.exclude(pk=old.pk).get(source=old.source, name=old.name)


def test_keys_of_an_instance_are_reissued_by_one_change(
    awg_server: FakeAwgServer, actions: KeyActions, run: Run, admin: User, start: Start
) -> None:
    olds = issued(actions, run, SOURCE, 3)
    awg_server.commands.clear()

    started = start(lambda: KeyReissue(admin).start(olds))

    (operation,) = started.operations
    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    assert operation.kind == REISSUE
    assert sum("syncconf" in command for command in awg_server.commands) == 1
    for old in olds:
        old.refresh_from_db()
        new = replacement(old)
        assert old.status == KeyStatus.REVOKED
        assert (old.private_key, old.profile) == ("", "")
        assert (new.status, new.user_id) == (KeyStatus.ACTIVE, old.user_id)
        assert new.public_key in awg_peers(awg_server)
        assert old.public_key not in awg_peers(awg_server)
    assert AuditEvent.objects.filter(action=REISSUE).count() == 3


def test_xray_restarts_once_for_the_batch(
    xray_server: FakeXrayServer, actions: KeyActions, run: Run, admin: User, start: Start
) -> None:
    olds = issued(actions, run, XRAY_SOURCE, 2)
    restarts = xray_server.restarts

    (operation,) = start(lambda: KeyReissue(admin).start(olds)).operations

    assert operation.status == OperationStatus.SUCCEEDED, operation.error_message
    assert xray_server.restarts == restarts + 1
    ids = XrayServerConfig.parse(xray_server.files[XRAY_CONFIG_PATH]).client_ids
    assert ids == [OWN_CLIENT, *(replacement(old).private_key for old in olds)]


def test_a_key_that_fails_keeps_its_old_one_and_is_named_in_the_log(
    xray_server: FakeXrayServer, actions: KeyActions, run: Run, admin: User, start: Start
) -> None:
    (good,) = issued(actions, run, XRAY_SOURCE, 1)
    own = AccessKey.objects.create(
        source=XRAY_SOURCE,
        name="owner",
        public_key=client_fingerprint(OWN_CLIENT),
        private_key=OWN_CLIENT,
        status=KeyStatus.ACTIVE,
        imported=True,
    )

    (operation,) = start(lambda: KeyReissue(admin).start([good, own])).operations

    assert operation.status == OperationStatus.FAILED
    assert operation.error_code == PeerErrorCode.NOT_ALL_REISSUED
    log = "\n".join(operation.steps.values_list("log", flat=True))
    assert "owner" in log
    own.refresh_from_db()
    good.refresh_from_db()
    assert own.status == KeyStatus.ACTIVE
    assert replacement(own).status == KeyStatus.ISSUING
    assert good.status == KeyStatus.REVOKED
    assert replacement(good).status == KeyStatus.ACTIVE
    # A retry takes up only the key still left.
    assert [pair.old for pair in reissue.pending(operation.params["pairs"])] == [own]


@pytest.mark.usefixtures("awg_server", "xray_server")
def test_keys_by_instance_get_an_operation_each(
    actions: KeyActions,
    run: Run,
    admin: User,
    start: Start,
) -> None:
    olds = [*issued(actions, run, SOURCE, 2), *issued(actions, run, XRAY_SOURCE, 1)]

    started = start(lambda: KeyReissue(admin).start(olds))

    assert sorted(op.params["source"] for op in started.operations) == [SOURCE, XRAY_SOURCE]
    assert all(op.status == OperationStatus.SUCCEEDED for op in started.operations)
    assert Operation.objects.filter(kind=REISSUE).count() == 2


def test_an_owner_who_may_not_have_keys_keeps_the_old_one(
    awg_server: FakeAwgServer, actions: KeyActions, run: Run, admin: User
) -> None:
    (old,) = issued(actions, run, SOURCE, 1)
    assert old.user is not None
    old.user.status = Status.BLOCKED
    old.user.save()

    started = KeyReissue(admin).start([old])

    assert started.kept == [old]
    assert (started.reissued, started.operations) == ([], [])


def test_a_new_key_revoked_meanwhile_is_not_issued(
    awg_server: FakeAwgServer, actions: KeyActions, run: Run
) -> None:
    (old,) = issued(actions, run, SOURCE, 1)
    new = AccessKey.objects.create(
        source=SOURCE, name="x", public_key="new", status=KeyStatus.REVOKED
    )

    assert reissue.pending([[old.pk, new.pk]]) == []


def test_old_keys_are_revoking_until_the_operation_ends_and_are_not_reissued_twice(
    awg_server: FakeAwgServer, actions: KeyActions, run: Run, admin: User
) -> None:
    (old,) = issued(actions, run, SOURCE, 1)

    first = KeyReissue(admin).start([old])
    second = KeyReissue(admin).start([old])

    old.refresh_from_db()
    assert old.status == KeyStatus.REVOKING
    assert first.operation_for(old).params["pairs"][0][2] == KeyStatus.ACTIVE
    assert (second.reissued, second.issued, second.operations) == ([], [], [])
