"""The flag of a running move operation for the host, and the heartbeat of a move preparation.

While a move operation runs, the panel keeps `UPDATE_DIR/request/moving`: "<move id> <until>",
with `until` in Unix time. deploy/update/lamnezia-deploy.sh refuses to rebuild the stack while the
flag is in force, since a rebuild would kill the operation halfway. The preparation renews the flag
as it goes, so a flag that ran out while its move still prepares means the worker died: the move
fails and the panel accepts changes again (`fail_interrupted_preparation`).

The copy on the new machine is restored with the move in PREPARING, but without the flag, which
lives on the host and not in the archive: the copy is never failed this way.
"""

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.db import transaction

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


def fail_interrupted_preparation(flag: MoveFlag | None = None) -> PanelMove | None:
    """Fail the preparation whose flag ran out: its worker stopped renewing it.

    A flag that ran out after another move operation is removed and changes nothing else.
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
        if move is None or move.state not in PanelMove.PREPARATION:
            return None
        move.mark(MoveState.FAILED)
        if move.operation_id is not None:
            fail_if_unfinished(move.operation_id)
    logger.warning("move %s failed: its preparation stopped answering", move_id)
    return move
