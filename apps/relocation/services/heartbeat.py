"""The flag of a running move operation for the host, and the heartbeat of a move preparation.

While a move operation runs, the panel keeps `UPDATE_DIR/request/moving`: "<move id> <until>",
with `until` in Unix time. deploy/update/lamnezia-deploy.sh refuses to rebuild the stack while the
flag is in force, since a rebuild would kill the operation halfway. The preparation renews the flag
as it goes, so a flag that ran out while its operation still runs means the worker died: the
operation fails, a preparation fails its move and the panel accepts changes again
(`fail_interrupted_move`). A confirmation or a cancel holds the flag for its longest command and
is failed the same way once it runs out.

An operation of a move that no worker took within `RELOCATION_QUEUE_TIMEOUT` fails too
(`fail_unstarted_move`): the worker does not run, and the panel should not wait an hour for
`fail_stale`.

The copy on the new machine is restored with the move in PREPARING, but without the flag, which
lives on the host and not in the archive: the copy is never failed this way.
"""

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction

from apps.operations.models import Operation, OperationStatus
from apps.operations.services.stale import fail_if_unfinished
from apps.relocation.errors import MoveError, MoveErrorCode
from apps.relocation.models import MoveState, PanelMove

logger = logging.getLogger(__name__)

FLAG_NAME = "moving"
# The host reads no more of the file than this.
_FLAG_LIMIT = 100


class MoveFlag:
    """The file that keeps the host from rebuilding the stack; the panel owns its folder."""

    def __init__(self, folder: Path) -> None:
        self._path = folder / FLAG_NAME

    @classmethod
    def from_settings(cls) -> "MoveFlag":
        return cls(Path(settings.UPDATE_DIR) / "request")

    def hold(self, move_id: int, seconds: int) -> None:
        """Keep the host from rebuilding for `seconds`; the file is replaced, never half-written."""
        until = int(time.time()) + seconds
        temporary = self._path.with_name(f".{FLAG_NAME}")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(f"{move_id} {until}\n", newline="\n")
            temporary.replace(self._path)
        except OSError as exc:
            # A panel without the host update unit has nothing to keep from rebuilding.
            logger.warning("move flag for the host not written: %s", exc)

    def read(self) -> tuple[int, int] | None:
        """The move and the Unix time the flag holds until; None without a valid flag."""
        try:
            fields = self._path.read_text()[:_FLAG_LIMIT].split()
        except OSError:
            return None
        if len(fields) != 2 or not all(field.isdigit() for field in fields):  # noqa: PLR2004
            return None
        return int(fields[0]), int(fields[1])

    def release(self, move_id: int) -> None:
        """Remove the flag of this move; a flag of a newer move stays."""
        held = self.read()
        if held is not None and held[0] != move_id:
            return
        try:
            self._path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("move flag for the host not removed: %s", exc)

    @contextmanager
    def held(self, move_id: int, seconds: int) -> Iterator[None]:
        self.hold(move_id, seconds)
        try:
            yield
        finally:
            self.release(move_id)


class PreparationHeartbeat:
    """Renews the flag while the preparation runs and stops a preparation already failed."""

    def __init__(self, move: PanelMove, flag: MoveFlag) -> None:
        self._move = move
        self._flag = flag

    def beat(self) -> None:
        with transaction.atomic():
            preparing = (
                PanelMove.objects.select_for_update()
                .filter(pk=self._move.pk, state__in=PanelMove.PREPARATION)
                .first()
            )
            if preparing is None:
                raise MoveError(MoveErrorCode.INTERRUPTED)
            self._flag.hold(self._move.pk, settings.RELOCATION_HEARTBEAT_TIMEOUT)

    @contextmanager
    def kept(self, every: float | None = None) -> Iterator[None]:
        """Renew the flag from a thread while one long call runs, such as writing the archive.

        The thread only rewrites the file: the state of the move is checked at the next `beat`.
        """
        stop = threading.Event()
        interval = every or settings.RELOCATION_HEARTBEAT_TIMEOUT / 3

        def renew() -> None:
            while not stop.wait(interval):
                self._flag.hold(self._move.pk, settings.RELOCATION_HEARTBEAT_TIMEOUT)

        thread = threading.Thread(target=renew, name=f"move-{self._move.pk}-pulse", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()


def fail_interrupted_move(flag: MoveFlag | None = None) -> PanelMove | None:
    """Fail the move operation whose flag ran out: its worker died or stopped renewing it.

    A preparation fails its move as well; after a confirmation or a cancel the move keeps its
    state, and its page offers them again.
    """
    flag = flag or MoveFlag.from_settings()
    held = flag.read()
    if held is None or held[1] > time.time():
        return None
    move_id = held[0]
    with transaction.atomic():
        # The row lock waits for a heartbeat under way; the flag it renews is read again below.
        move = PanelMove.objects.select_for_update().filter(pk=move_id).first()
        if flag.read() != held:
            return None
        flag.release(move_id)
        if move is None:
            return None
        failed = move.operation_id is not None and fail_if_unfinished(move.operation_id)
        if move.state in PanelMove.PREPARATION:
            move.mark(MoveState.FAILED)
        elif not failed:
            return None
    logger.warning("move %s: its operation stopped answering", move_id)
    return move


def fail_unstarted_move(now: datetime) -> PanelMove | None:
    """Fail the operation of the latest move that waits in the queue too long; None if none does.

    A confirmation that never started is not a partly confirmed move: its mark is taken back.
    """
    cutoff = now - timedelta(seconds=settings.RELOCATION_QUEUE_TIMEOUT)
    with transaction.atomic():
        move = (
            PanelMove.objects.select_for_update(of=("self",))
            .select_related("operation")
            .order_by("-pk")
            .first()
        )
        operation = move.operation if move else None
        if move is None or operation is None or not _queued_before(operation, cutoff):
            return None
        if not fail_if_unfinished(operation.pk):
            return None
        if move.state in PanelMove.PREPARATION:
            move.mark(MoveState.FAILED)
        if move.confirm_started and move.state == MoveState.READY:
            move.confirm_started = False
            move.save(update_fields=["confirm_started", "updated_at"])
    logger.warning("move %s: no worker took its operation", move.pk)
    return move


def _queued_before(operation: Operation, cutoff: datetime) -> bool:
    return operation.status == OperationStatus.PENDING and operation.created_at < cutoff
