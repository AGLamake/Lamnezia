import time
from pathlib import Path

import pytest

from apps.operations.models import OperationStatus
from apps.operations.services.context import OperationContext
from apps.operations.tests.factories import OperationFactory
from apps.relocation.errors import MoveError, MoveErrorCode
from apps.relocation.models import MoveState, PanelMove
from apps.relocation.services.freeze import TaskGate, panel_frozen
from apps.relocation.services.heartbeat import (
    MoveFlag,
    PreparationHeartbeat,
    fail_interrupted_preparation,
)
from apps.relocation.services.preparation import MovePreparation
from apps.relocation.tasks import FAIL_INTERRUPTED_TASK, fail_interrupted
from apps.relocation.tests.helpers import FakeMachine, fake_connect, make_move
from apps.relocation.tests.test_flow import PREPARATION, FakeGuard

pytestmark = pytest.mark.django_db


@pytest.fixture
def flag(update_dir: Path) -> MoveFlag:
    return MoveFlag.from_settings()


def flag_file(update_dir: Path) -> Path:
    return update_dir / "request" / "moving"


def ran_out(flag: MoveFlag, move: PanelMove) -> None:
    flag.hold(move.pk, -1)


def test_the_flag_names_the_move_and_its_time(flag: MoveFlag, update_dir: Path) -> None:
    flag.hold(7, 600)

    move_id, until = flag.read() or (0, 0)
    assert move_id == 7
    assert time.time() + 590 < until <= time.time() + 600
    assert flag_file(update_dir).read_text() == f"7 {until}\n"


def test_a_move_removes_only_its_own_flag(flag: MoveFlag) -> None:
    flag.hold(8, 600)

    flag.release(7)
    assert flag.read() is not None

    flag.release(8)
    assert flag.read() is None


def test_a_broken_flag_counts_for_nothing(flag: MoveFlag, update_dir: Path) -> None:
    flag_file(update_dir).parent.mkdir(parents=True)
    flag_file(update_dir).write_text("7; rm -rf /\n")

    assert flag.read() is None
    assert fail_interrupted_preparation(flag) is None


def test_the_preparation_holds_the_flag_while_it_runs(
    monkeypatch: pytest.MonkeyPatch, flag: MoveFlag, tmp_path: Path, settings: object
) -> None:
    settings.PANEL_ARCHIVE_DIR = tmp_path  # type: ignore[attr-defined]
    move = make_move()
    seen: list[tuple[int, int] | None] = []
    fake = FakeMachine(during_install=lambda: seen.append(flag.read()))

    run_preparation(monkeypatch, fake, move)

    assert seen[0] is not None and seen[0][0] == move.pk
    assert fake.beat is not None
    assert flag.read() is None
    assert PanelMove.objects.get().state == MoveState.READY


def run_preparation(monkeypatch: pytest.MonkeyPatch, fake: FakeMachine, move: PanelMove) -> None:
    fake_connect(monkeypatch, PREPARATION)
    monkeypatch.setattr(f"{PREPARATION}.TargetMachine", fake)
    monkeypatch.setattr(f"{PREPARATION}.RelayGuard", FakeGuard)
    monkeypatch.setattr(
        f"{PREPARATION}.write_code_bundle", lambda root, path: path.write_bytes(b"code")
    )

    def write_archive(password: str, path: Path) -> None:
        path.write_bytes(b"archive")

    MovePreparation(move, OperationContext(OperationFactory()), write_archive).run()


@pytest.mark.parametrize("state", [MoveState.CHECKING, MoveState.PREPARING])
def test_a_silent_preparation_fails_and_the_panel_accepts_changes(
    flag: MoveFlag, state: MoveState
) -> None:
    move = make_move(state, OperationStatus.RUNNING)
    ran_out(flag, move)

    assert fail_interrupted_preparation(flag) == move

    move.refresh_from_db()
    assert move.state == MoveState.FAILED
    assert move.operation is not None
    move.operation.refresh_from_db()
    assert (move.operation.status, move.operation.error_code) == (
        OperationStatus.FAILED,
        "interrupted",
    )
    assert flag.read() is None
    assert not panel_frozen()
    assert move.can_cancel


def test_a_preparation_that_beats_is_left_alone(flag: MoveFlag) -> None:
    move = make_move(operation_status=OperationStatus.RUNNING)
    PreparationHeartbeat(move, flag).beat()

    assert fail_interrupted_preparation(flag) is None
    assert PanelMove.objects.get().state == MoveState.PREPARING


def test_the_copy_on_the_new_machine_is_never_failed(flag: MoveFlag) -> None:
    """The copy holds the move in PREPARING from the archive but has no flag of its own."""
    make_move(operation_status=OperationStatus.RUNNING)

    assert fail_interrupted_preparation(flag) is None
    assert PanelMove.objects.get().state == MoveState.PREPARING


def test_a_flag_left_by_another_step_is_only_removed(flag: MoveFlag) -> None:
    move = make_move(MoveState.READY, OperationStatus.RUNNING, confirm_started=True)
    ran_out(flag, move)

    assert fail_interrupted_preparation(flag) is None
    assert flag.read() is None
    assert PanelMove.objects.get().state == MoveState.READY


def test_a_late_worker_stops_at_its_next_beat(flag: MoveFlag) -> None:
    move = make_move(MoveState.FAILED)

    with pytest.raises(MoveError) as error:
        PreparationHeartbeat(move, flag).beat()
    assert error.value.code == MoveErrorCode.INTERRUPTED
    assert flag.read() is None


def test_a_late_worker_never_overwrites_a_cancel(
    monkeypatch: pytest.MonkeyPatch, flag: MoveFlag, tmp_path: Path, settings: object
) -> None:
    """The move failed for silence and was cancelled while its worker still installed."""
    settings.PANEL_ARCHIVE_DIR = tmp_path  # type: ignore[attr-defined]
    move = make_move()

    def cancel() -> None:
        PanelMove.objects.filter(pk=move.pk).update(state=MoveState.CANCELLED)

    fake = FakeMachine(during_install=cancel)

    with pytest.raises(MoveError) as error:
        run_preparation(monkeypatch, fake, move)

    assert error.value.code == MoveErrorCode.INTERRUPTED
    assert "discard" in fake.calls
    assert PanelMove.objects.get().state == MoveState.CANCELLED


def test_the_check_runs_while_the_panel_is_frozen(flag: MoveFlag) -> None:
    move = make_move(operation_status=OperationStatus.RUNNING)
    ran_out(flag, move)

    assert TaskGate().allows(FAIL_INTERRUPTED_TASK, ())
    assert fail_interrupted() == move.pk
