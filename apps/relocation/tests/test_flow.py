from pathlib import Path

import pytest

from apps.operations.models import Operation, OperationStatus, OperationStep
from apps.operations.services.context import OperationContext
from apps.operations.tests.factories import OperationFactory
from apps.relocation.errors import MoveError, MoveErrorCode
from apps.relocation.models import MoveState, PanelMove
from apps.relocation.services.completion import MoveCancellation, MoveConfirmation
from apps.relocation.services.preparation import MovePreparation
from apps.relocation.tests.helpers import PASSWORD_LENGTH, FakeMachine, fake_connect, make_move
from apps.ssh.errors import ErrorCode, SshError

pytestmark = pytest.mark.django_db

PREPARATION = "apps.relocation.services.preparation"
COMPLETION = "apps.relocation.services.completion"


class FakeGuard:
    def __init__(self, host: object, ctx: object) -> None:
        pass

    def remember(self) -> None:
        pass

    def check(self) -> None:
        pass


@pytest.fixture
def staging(settings: object, tmp_path: Path) -> Path:
    settings.PANEL_ARCHIVE_DIR = tmp_path  # type: ignore[attr-defined]
    return tmp_path


def prepare(
    monkeypatch: pytest.MonkeyPatch, fake: FakeMachine, move: PanelMove | None = None
) -> PanelMove:
    move = move or make_move()
    fake_connect(monkeypatch, PREPARATION)
    monkeypatch.setattr(f"{PREPARATION}.TargetMachine", fake)
    monkeypatch.setattr(f"{PREPARATION}.RelayGuard", FakeGuard)
    monkeypatch.setattr(
        f"{PREPARATION}.write_code_bundle", lambda root, path: path.write_bytes(b"code")
    )

    def write_archive(password: str, path: Path) -> None:
        path.write_bytes(b"archive")

    ctx = OperationContext(OperationFactory())
    try:
        MovePreparation(move, ctx, write_archive).run()
    finally:
        move.refresh_from_db()
    return move


def test_the_copy_is_installed_and_checked(monkeypatch: pytest.MonkeyPatch, staging: Path) -> None:
    fake = FakeMachine()

    move = prepare(monkeypatch, fake)

    assert move.state == MoveState.READY
    assert fake.calls == [
        "check",
        "upload_code",
        "upload_archive",
        "install",
        "remove_uploads",
        "verify",
    ]
    assert len(fake.password) == PASSWORD_LENGTH
    assert fake.password not in "".join(OperationStep.objects.values_list("log", flat=True))
    assert not list(staging.iterdir())


def test_a_failed_install_removes_the_copy(monkeypatch: pytest.MonkeyPatch, staging: Path) -> None:
    fake = FakeMachine(fail_at="install")

    with pytest.raises(MoveError):
        prepare(monkeypatch, fake)

    assert "discard" in fake.calls
    assert PanelMove.objects.get().state == MoveState.FAILED
    assert not list(staging.iterdir())


def test_a_refused_machine_is_left_alone(monkeypatch: pytest.MonkeyPatch, staging: Path) -> None:
    fake = FakeMachine(fail_at="check")

    with pytest.raises(MoveError):
        prepare(monkeypatch, fake)

    assert fake.calls == ["check"]


def test_the_panel_freezes_only_after_the_machine_fits(
    monkeypatch: pytest.MonkeyPatch, staging: Path
) -> None:
    frozen: list[bool] = []

    def record() -> None:
        frozen.append(PanelMove.objects.get().frozen)

    fake = FakeMachine(during_check=record, during_install=record)
    monkeypatch.setattr(f"{PREPARATION}.ready_image", lambda: "1.4.2")

    move = prepare(monkeypatch, fake, make_move(MoveState.CHECKING))

    assert move.state == MoveState.READY
    assert frozen == [False, True]
    assert fake.image == "1.4.2"


def test_an_unfit_machine_fails_the_move_before_the_freeze(
    monkeypatch: pytest.MonkeyPatch, staging: Path
) -> None:
    fake = FakeMachine(fail_at="check")

    with pytest.raises(MoveError):
        prepare(monkeypatch, fake, make_move(MoveState.CHECKING))

    move = PanelMove.objects.get()
    assert move.state == MoveState.FAILED
    assert not move.frozen
    assert fake.calls == ["check"]


def test_queued_jobs_are_refused_and_a_running_one_stops_the_move(
    monkeypatch: pytest.MonkeyPatch, staging: Path
) -> None:
    queued = Operation.objects.create(kind="keys.issue")
    Operation.objects.create(kind="servers.check", status=OperationStatus.RUNNING)

    with pytest.raises(MoveError) as error:
        prepare(monkeypatch, FakeMachine())

    assert error.value.code == MoveErrorCode.BUSY
    queued.refresh_from_db()
    assert queued.status == OperationStatus.FAILED


def finish(
    monkeypatch: pytest.MonkeyPatch, runner: type, fake: FakeMachine, move: PanelMove
) -> PanelMove:
    fake_connect(monkeypatch, COMPLETION)
    monkeypatch.setattr(f"{COMPLETION}.TargetMachine", fake)
    return run(runner, move)


def run(runner: type, move: PanelMove) -> PanelMove:
    try:
        runner(move, OperationContext(OperationFactory())).run()
    finally:
        move.refresh_from_db()
    return move


def test_confirming_lets_the_copy_take_over(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeMachine()

    move = finish(monkeypatch, MoveConfirmation, fake, make_move(MoveState.READY))

    assert move.state == MoveState.LEFT
    assert fake.calls == ["copy_state", "arrive"]


def test_a_copy_in_a_strange_state_is_not_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeMachine(copy=None)

    with pytest.raises(MoveError):
        finish(monkeypatch, MoveConfirmation, fake, make_move(MoveState.READY))

    assert PanelMove.objects.get().state == MoveState.READY


def test_cancelling_removes_the_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeMachine()

    move = finish(monkeypatch, MoveCancellation, fake, make_move(MoveState.READY))

    assert move.state == MoveState.CANCELLED
    assert fake.calls == ["copy_state", "discard", "remove_uploads"]


def test_an_unreachable_copy_still_unfreezes_the_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeMachine(fail_at="discard")

    with pytest.raises(MoveError) as error:
        finish(monkeypatch, MoveCancellation, fake, make_move(MoveState.FAILED))

    assert error.value.code == MoveErrorCode.NOT_DISCARDED
    assert PanelMove.objects.get().state == MoveState.CANCELLED


def test_a_copy_that_took_over_is_never_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeMachine(copy=MoveState.ARRIVED)

    with pytest.raises(MoveError) as error:
        finish(monkeypatch, MoveCancellation, fake, make_move(MoveState.READY))

    assert error.value.code == MoveErrorCode.ARRIVED
    assert "discard" not in fake.calls
    assert PanelMove.objects.get().state == MoveState.READY


def test_after_a_started_confirmation_a_failed_cancel_keeps_the_freeze(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeMachine(fail_at="discard")
    move = make_move(MoveState.READY, confirm_started=True)

    with pytest.raises(MoveError) as error:
        finish(monkeypatch, MoveCancellation, fake, move)

    assert PanelMove.objects.get().state == MoveState.READY
    assert error.value.code == MoveErrorCode.PARTLY_CONFIRMED


def unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def connect(server: object) -> None:
        raise SshError(ErrorCode.HOST_UNREACHABLE)

    monkeypatch.setattr(f"{COMPLETION}.connect", connect)


def test_a_partly_confirmed_move_keeps_both_ways_out_while_the_target_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unreachable(monkeypatch)
    move = make_move(MoveState.READY, confirm_started=True)

    with pytest.raises(MoveError) as error:
        run(MoveCancellation, move)
    assert error.value.code == MoveErrorCode.PARTLY_CONFIRMED

    with pytest.raises(SshError):
        run(MoveConfirmation, move)

    assert move.state == MoveState.READY
    assert (move.can_confirm, move.can_abandon) == (True, True)


def test_a_retried_confirmation_takes_over_once_the_target_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeMachine(copy=MoveState.ARRIVED)
    move = make_move(MoveState.READY, confirm_started=True)

    move = finish(monkeypatch, MoveConfirmation, fake, move)

    assert move.state == MoveState.LEFT
    assert fake.calls == ["copy_state", "arrive"]
