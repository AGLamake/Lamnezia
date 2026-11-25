"""Shared pieces of the panel move tests: a move in a given state and a fake new machine."""

from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest

from apps.operations.models import Operation, OperationStatus
from apps.relocation.errors import MoveError, MoveErrorCode
from apps.relocation.models import MoveState, PanelMove
from apps.servers.tests.factories import ServerFactory

PASSWORD_LENGTH = 43


def make_move(
    state: MoveState = MoveState.PREPARING,
    operation_status: OperationStatus | None = None,
    **fields: Any,
) -> PanelMove:
    target = ServerFactory()
    operation = None
    if operation_status is not None:
        operation = Operation.objects.create(kind="relocation.prepare", status=operation_status)
    return PanelMove.objects.create(
        target=target, target_host=target.host, state=state, operation=operation, **fields
    )


class FakeMachine:
    """Stands in for TargetMachine: records the calls, fails at the method it is told to."""

    def __init__(
        self,
        fail_at: str | None = None,
        copy: str | None = MoveState.PREPARING,
        during_install: Callable[[], None] | None = None,
        during_check: Callable[[], None] | None = None,
    ) -> None:
        self.fail_at = fail_at
        self.copy = copy
        self.during_install = during_install
        self.during_check = during_check
        self.image: str | None = None
        self.calls: list[str] = []
        self.password = ""
        self.beat: Callable[[], None] | None = None

    def __call__(
        self, host: object, ctx: object, move_id: int, beat: Callable[[], None] | None = None
    ) -> "FakeMachine":
        self.beat = beat
        return self

    def _do(self, name: str) -> None:
        self.calls.append(name)
        if name == self.fail_at:
            raise MoveError(MoveErrorCode.INSTALL_FAILED)

    def check(self) -> None:
        if self.during_check is not None:
            self.during_check()
        self._do("check")

    def upload_code(self, bundle: Path) -> None:
        assert bundle.exists()
        self._do("upload_code")

    def upload_archive(self, archive: Path) -> str:
        assert archive.exists()
        self._do("upload_archive")
        return "/var/tmp/panel.lamnezia"

    def install(self, archive: str, password: str, image: str | None = None) -> None:
        self.password = password
        self.image = image
        if self.during_install is not None:
            self.during_install()
        self._do("install")

    def verify(self, expected_state: str) -> None:
        self._do("verify")

    def verify_live(self) -> None:
        self._do("verify_live")

    def copy_state(self) -> str | None:
        self._do("copy_state")
        return self.copy

    def arrive(self) -> None:
        self._do("arrive")

    def discard(self) -> None:
        self._do("discard")

    def remove_uploads(self) -> None:
        self._do("remove_uploads")


def fake_connect(monkeypatch: pytest.MonkeyPatch, module: str) -> None:
    monkeypatch.setattr(f"{module}.connect", lambda server: nullcontext(object()))
