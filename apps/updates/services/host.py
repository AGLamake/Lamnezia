"""Files shared with the host for a release update: the request the panel leaves, the status back.

The panel has no access to Docker or to the host. It writes `request/update` with
"vX.Y.Z <run>", and lamnezia-update.path of install.sh starts deploy/update/lamnezia-deploy.sh
release, which writes `status/status`, `status/log` and `status/agent` (the branch the
installation follows). The same script keeps `status/summary` while install-summary.txt still
holds the first password. `status` is mounted read-only into the panel.
"""

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Self

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.updates.services.versions import Version

_CHANNEL = re.compile(r"[\w.\-/]{1,60}")
_RUN = re.compile(r"[0-9a-f]{32}")
# The end of the log is enough to see what happened; the whole is on the host.
_LOG_TAIL_BYTES = 32 * 1024


class UpdateState(StrEnum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


_STATE_LABELS = {
    UpdateState.RUNNING: _("running"),
    UpdateState.DONE: _("done"),
    UpdateState.FAILED: _("failed"),
    UpdateState.ROLLED_BACK: _("rolled back"),
}
_BADGES = {
    UpdateState.RUNNING: "badge-muted",
    UpdateState.DONE: "badge-success",
    UpdateState.FAILED: "badge-danger",
    UpdateState.ROLLED_BACK: "badge-warning",
}


@dataclass(frozen=True)
class HostStatus:
    """The last update the host ran, as its script wrote it."""

    run: str
    state: UpdateState
    reason: str
    version: str
    previous: str
    started: datetime | None
    finished: datetime | None

    @classmethod
    def parse(cls, text: str) -> Self | None:
        values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        try:
            state = UpdateState(values.get("state", ""))
        except ValueError:
            return None
        return cls(
            run=values.get("run", ""),
            state=state,
            reason=values.get("reason", ""),
            version=values.get("version", ""),
            previous=values.get("previous", ""),
            started=_moment(values.get("started", "")),
            finished=_moment(values.get("finished", "")),
        )

    @property
    def running(self) -> bool:
        return self.state == UpdateState.RUNNING

    @property
    def badge(self) -> str:
        """CSS class of the state badge."""
        return _BADGES[self.state]

    @property
    def state_label(self) -> str:
        return str(_STATE_LABELS[self.state])


class HostUpdater:
    """The panel side of the exchange; `available` only where install.sh set up the host unit."""

    REQUEST = "update"

    def __init__(self, root: Path, timeout: timedelta) -> None:
        self._requests = root / "request"
        self._status = root / "status"
        self._timeout = timeout

    @classmethod
    def from_settings(cls) -> Self:
        return cls(settings.UPDATE_DIR, timedelta(seconds=settings.UPDATE_TIMEOUT))

    @property
    def available(self) -> bool:
        """The host unit is installed and the panel may leave a request."""
        return (
            self._agent.is_file() and self._requests.is_dir() and os.access(self._requests, os.W_OK)
        )

    def channel(self) -> str:
        """The branch the installation follows: releases, or dev for a development one."""
        line = _read(self._agent).strip()
        return line if _CHANNEL.fullmatch(line) else ""

    def summary_left(self) -> bool:
        """install-summary.txt on the host still holds the first administrator password."""
        return (self._status / "summary").is_file()

    def status(self) -> HostStatus | None:
        return HostStatus.parse(_read(self._status / "status"))

    def log(self) -> str:
        """The end of the log of the last update."""
        try:
            with (self._status / "log").open("rb") as file:
                size = file.seek(0, os.SEEK_END)
                file.seek(max(size - _LOG_TAIL_BYTES, 0))
                return file.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def pending(self) -> datetime | None:
        """When the request the host has not taken yet was left."""
        try:
            moment = (self._requests / self.REQUEST).stat().st_mtime
        except OSError:
            return None
        return datetime.fromtimestamp(moment, tz=UTC)

    def stuck_request(self) -> bool:
        """A request left long ago and never taken: the host unit does not run."""
        pending = self.pending()
        return pending is not None and not self._recent(pending)

    def busy(self) -> bool:
        """An update is asked for or running; a stuck one does not hold the next."""
        pending = self.pending()
        if pending is not None and self._recent(pending):
            return True
        status = self.status()
        return bool(status and status.running and status.started and self._recent(status.started))

    def request(self, version: Version, run: str) -> None:
        """Leaves the request whole: the host never reads half of it."""
        if not _RUN.fullmatch(run):
            raise ValueError(f"bad run id {run!r}")
        handle, name = tempfile.mkstemp(dir=self._requests, prefix=".request-")
        try:
            with os.fdopen(handle, "w", encoding="ascii") as file:
                file.write(f"{version.tag} {run}\n")
            os.replace(name, self._requests / self.REQUEST)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise

    @property
    def _agent(self) -> Path:
        return self._status / "agent"

    def _recent(self, moment: datetime) -> bool:
        return timezone.now() - moment < self._timeout


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return ""


def _moment(text: str) -> datetime | None:
    return datetime.fromtimestamp(int(text), tz=UTC) if text.isdigit() else None
