import os
import time
from datetime import timedelta
from pathlib import Path

import pytest

from apps.updates.errors import host_reason
from apps.updates.services.host import HostStatus, HostUpdater, UpdateState
from apps.updates.services.versions import Version

RUN = "0123456789abcdef0123456789abcdef"


def host(root: Path) -> HostUpdater:
    return HostUpdater(root, timedelta(hours=1))


def age(path: Path, seconds: int) -> None:
    moment = time.time() - seconds
    os.utime(path, (moment, moment))


def test_without_the_host_unit_there_is_no_update(tmp_path: Path) -> None:
    root = tmp_path / "update"
    (root / "request").mkdir(parents=True)
    (root / "status").mkdir()

    assert not host(root).available
    (root / "status" / "agent").write_text("dev\n")
    assert host(root).available
    assert host(root).channel() == "dev"


def test_the_request_is_written_whole(update_dir: Path) -> None:
    updater = host(update_dir)

    updater.request(Version(1, 2, 0), RUN)

    assert (update_dir / "request" / "update").read_text() == f"v1.2.0 {RUN}\n"
    assert [path.name for path in (update_dir / "request").iterdir()] == ["update"]
    assert updater.busy()
    with pytest.raises(ValueError, match="run id"):
        updater.request(Version(1, 2, 0), "../../etc")


def test_a_request_not_taken_for_an_hour_is_stuck(update_dir: Path) -> None:
    updater = host(update_dir)
    updater.request(Version(1, 2, 0), RUN)
    age(update_dir / "request" / "update", 2 * 3600)

    assert updater.stuck_request()
    assert not updater.busy()


def test_status_and_log_from_the_host(update_dir: Path) -> None:
    started = int(time.time()) - 60
    (update_dir / "status" / "status").write_text(
        f"run={RUN}\nstate=rolled_back\nreason=unhealthy\nversion=v1.2.0\n"
        f"previous=v1.1.0\nstarted={started}\nfinished={started + 30}\n"
    )
    (update_dir / "status" / "log").write_bytes(b"x" * 40000 + b"\nthe end\n")
    updater = host(update_dir)

    status = updater.status()

    assert status is not None
    assert status.state == UpdateState.ROLLED_BACK
    assert (status.version, status.previous, status.run) == ("v1.2.0", "v1.1.0", RUN)
    assert status.finished and status.started and status.finished > status.started
    assert host_reason(status.reason) == "The new version did not answer the health check."
    assert updater.log().endswith("the end\n")
    assert len(updater.log()) <= 32 * 1024
    assert not updater.busy()


def test_a_running_update_holds_the_next_until_the_timeout(update_dir: Path) -> None:
    status = update_dir / "status" / "status"
    status.write_text(f"state=running\nstarted={int(time.time())}\n")
    assert host(update_dir).busy()

    status.write_text(f"state=running\nstarted={int(time.time()) - 2 * 3600}\n")
    assert not host(update_dir).busy()


def test_an_unknown_status_is_ignored() -> None:
    assert HostStatus.parse("state=exploded\n") is None
    assert HostStatus.parse("") is None
    assert host_reason("something_new") == "something_new"


def test_the_host_tells_whether_the_summary_still_holds_the_password(update_dir: Path) -> None:
    assert not host(update_dir).summary_left()
    (update_dir / "status" / "summary").touch()
    assert host(update_dir).summary_left()
