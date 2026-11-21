from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from django.test import Client
from django.utils import timezone

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.backups.errors import PanelArchiveError, PanelArchiveErrorCode
from apps.backups.operations import archive_panel
from apps.backups.services import archive_remote
from apps.backups.services.archive_schedule import ArchiveSchedule
from apps.backups.services.panel_archive import (
    ARCHIVE_PASSWORD_VARIABLE,
    PanelArchiveReader,
    env_file,
)
from apps.backups.services.panel_archives import PANEL_ARCHIVE_KIND, SCHEDULED_PARAM
from apps.backups.tasks import archive_panel_on_schedule
from apps.backups.tests.panel_archive_helpers import PASSWORD, FakeDump, folder
from apps.notifications.events import Event
from apps.notifications.models import Notification
from apps.notifications.tests.factories import ChannelFactory
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.context import OperationContext

NOW = timezone.now()
DAILY = ArchiveSchedule(PASSWORD, timedelta(hours=24))


@pytest.fixture
def scheduled(settings: Any, archive_settings: Path, fake_dump: FakeDump) -> Path:
    settings.PANEL_ARCHIVE_PASSWORD = PASSWORD
    settings.PANEL_ARCHIVE_INTERVAL = 24
    settings.PANEL_ARCHIVE_REMOTE = ""
    return archive_settings


class FakeRemote:
    label = "s3://backups/"

    def __init__(self, error: PanelArchiveError | None = None) -> None:
        self.error = error
        self.sent: list[Path] = []

    def send(self, path: Path) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append(path)


def test_the_archive_password_stays_out_of_the_archive() -> None:
    env = env_file({ARCHIVE_PASSWORD_VARIABLE: PASSWORD, "LAMNEZIA_PANEL_ARCHIVE_KEEP": "3"})

    assert env == "LAMNEZIA_PANEL_ARCHIVE_KEEP=3\n"


@pytest.mark.parametrize(
    ("newest", "due"),
    [
        (None, True),
        (NOW - timedelta(hours=24), True),
        (NOW - timedelta(hours=23, minutes=40), True),
        (NOW - timedelta(hours=23), False),
    ],
)
def test_an_archive_is_due_by_the_newest_one(newest: Any, due: bool) -> None:
    assert DAILY.due(newest, NOW) is due


def test_the_schedule_is_off_without_a_password() -> None:
    schedule = ArchiveSchedule("", timedelta(hours=24))

    assert not schedule.enabled
    assert not schedule.due(None, NOW)


def test_a_short_password_makes_no_archive() -> None:
    with pytest.raises(PanelArchiveError) as error:
        ArchiveSchedule("short", timedelta(hours=1)).checked_password()

    assert error.value.code == PanelArchiveErrorCode.SHORT_PASSWORD


@pytest.mark.django_db
def test_beat_queues_one_archive_without_its_password(scheduled: Path) -> None:
    archive_panel_on_schedule()
    archive_panel_on_schedule()

    (operation,) = Operation.objects.filter(kind=PANEL_ARCHIVE_KIND)
    assert operation.params == {SCHEDULED_PARAM: True}


@pytest.mark.django_db
def test_beat_waits_for_the_interval(scheduled: Path) -> None:
    folder().create(PASSWORD)

    archive_panel_on_schedule()

    assert not Operation.objects.exists()


@pytest.mark.django_db
def test_a_scheduled_archive_is_made_and_sent(
    scheduled: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = FakeRemote()
    monkeypatch.setattr("apps.backups.operations.remote_from_settings", lambda: remote)
    operation = Operation.objects.create(kind=PANEL_ARCHIVE_KIND, params={SCHEDULED_PARAM: True})

    archive_panel(OperationContext(operation))

    (archive,) = folder().archives()
    assert remote.sent == [archive.path]
    assert PanelArchiveReader(PASSWORD).check(archive.path)
    assert PASSWORD not in "".join(step.log for step in operation.steps.all())


@pytest.mark.django_db
def test_a_failed_copy_keeps_the_archive_and_tells_the_staff(
    scheduled: Path, monkeypatch: pytest.MonkeyPatch, django_capture_on_commit_callbacks: Any
) -> None:
    ChannelFactory(events=[Event.PANEL_ARCHIVE_FAILED])
    error = PanelArchiveError(PanelArchiveErrorCode.REMOTE_LOGIN, "403 InvalidAccessKeyId")
    monkeypatch.setattr("apps.backups.operations.remote_from_settings", lambda: FakeRemote(error))
    operation = Operation.objects.create(kind=PANEL_ARCHIVE_KIND, params={SCHEDULED_PARAM: True})

    with django_capture_on_commit_callbacks(execute=True), pytest.raises(PanelArchiveError):
        archive_panel(OperationContext(operation))

    assert len(folder().archives()) == 1
    assert "403 InvalidAccessKeyId" in "".join(step.log for step in operation.steps.all())
    (notice,) = Notification.objects.all()
    assert notice.event == Event.PANEL_ARCHIVE_FAILED
    assert notice.data["error"]


@pytest.mark.django_db
def test_wrong_storage_settings_still_leave_the_archive(scheduled: Path, settings: Any) -> None:
    settings.PANEL_ARCHIVE_REMOTE = "ftp://nowhere/"
    operation = Operation.objects.create(kind=PANEL_ARCHIVE_KIND, params={SCHEDULED_PARAM: True})

    with pytest.raises(PanelArchiveError) as error:
        archive_panel(OperationContext(operation))

    assert error.value.code == PanelArchiveErrorCode.REMOTE_INVALID
    assert len(folder().archives()) == 1


@pytest.mark.django_db
def test_the_page_shows_the_schedule_and_the_last_failure(
    client: Client, scheduled: Path, settings: Any
) -> None:
    settings.PANEL_ARCHIVE_REMOTE = "s3://backups/panel"
    settings.PANEL_ARCHIVE_REMOTE_ACCESS_KEY = "access"
    settings.PANEL_ARCHIVE_REMOTE_SECRET_KEY = "s3-key-value"
    failed = Operation.objects.create(kind=PANEL_ARCHIVE_KIND)
    failed.finish(OperationStatus.FAILED, message="Could not log in to the external storage.")
    client.force_login(UserFactory(role=Role.ADMIN))

    page = client.get("/backups/panel/").content.decode()

    assert "every 24 hours" in page
    assert "s3://backups/panel/" in page
    assert "Could not log in to the external storage." in page
    assert PASSWORD not in page
    assert "s3-key-value" not in page


@pytest.mark.django_db
def test_a_success_hides_the_failure(client: Client, archive_settings: Path) -> None:
    Operation.objects.create(kind=PANEL_ARCHIVE_KIND).finish(OperationStatus.FAILED, message="boom")
    Operation.objects.create(kind=PANEL_ARCHIVE_KIND).finish(OperationStatus.SUCCEEDED)
    client.force_login(UserFactory(role=Role.ADMIN))

    assert "boom" not in client.get("/backups/panel/").content.decode()


def test_the_storage_module_never_logs() -> None:
    assert "logging" not in Path(archive_remote.__file__).read_text(encoding="utf-8")
