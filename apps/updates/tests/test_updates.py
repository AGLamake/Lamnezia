import time
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from apps.accounts.models import Role
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.backups.errors import PanelArchiveError
from apps.backups.services.database_dump import PostgresDump
from apps.backups.services.panel_archives import PASSWORD_PARAM
from apps.backups.tests.panel_archive_helpers import PASSWORD, FakeDump, folder
from apps.dashboard.services.attention import Attention
from apps.guides.models import Release
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.context import OperationContext
from apps.updates.errors import UpdateError, UpdateErrorCode
from apps.updates.operations import update_panel
from apps.updates.services.updates import RUN_PARAM, UPDATE_KIND, PanelUpdate

pytestmark = pytest.mark.django_db


@pytest.fixture
def released() -> Release:
    return Release.objects.create(
        repository=settings.UPDATE_REPOSITORY, payload={"tag_name": "v1.1.0"}
    )


@pytest.fixture
def archives(settings: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    settings.PANEL_ARCHIVE_DIR = tmp_path / "archives"
    settings.MEDIA_ROOT = tmp_path / "media"
    monkeypatch.setattr(PostgresDump, "from_settings", staticmethod(FakeDump))
    return tmp_path / "archives"


def start(version: str = "1.1.0") -> Operation:
    return PanelUpdate.from_settings().start(version, PASSWORD, UserFactory(role=Role.ADMIN), None)


def test_only_a_newer_release_is_offered(released: Release) -> None:
    assert str(PanelUpdate.from_settings().offer().release.version) == "1.1.0"  # type: ignore[union-attr]

    released.payload = {"tag_name": "v1.0.0"}
    released.save()
    cache.clear()
    assert PanelUpdate.from_settings().offer().release is None


def test_start_is_refused_without_the_host_unit(released: Release, settings: Any) -> None:
    settings.UPDATE_DIR = Path(settings.BASE_DIR) / "no-such-update-dir"

    with pytest.raises(UpdateError) as error:
        start()

    assert error.value.code == UpdateErrorCode.NO_HOST


def test_only_the_offered_version_starts(released: Release, update_dir: Path) -> None:
    for version in ("1.0.0", "0.9.0", "1.2.0", "garbage"):
        with pytest.raises(UpdateError) as error:
            start(version)
        assert error.value.code == UpdateErrorCode.NOT_OFFERED
    assert not Operation.objects.exists()


def test_one_update_at_a_time(released: Release, update_dir: Path) -> None:
    operation = start()

    assert PASSWORD not in str(operation.params)
    assert operation.params[PASSWORD_PARAM]
    event = AuditEvent.objects.get(action=UPDATE_KIND)
    assert event.data == {"from": "1.0.0", "to": "1.1.0"}
    with pytest.raises(UpdateError) as error:
        start()
    assert error.value.code == UpdateErrorCode.BUSY

    operation.status = OperationStatus.SUCCEEDED
    operation.save()
    (update_dir / "status" / "status").write_text(f"state=running\nstarted={int(time.time())}\n")
    with pytest.raises(UpdateError):
        start()


def test_the_operation_archives_the_panel_then_asks_the_host(
    released: Release, update_dir: Path, archives: Path
) -> None:
    operation = start()

    update_panel(OperationContext(operation))

    operation.refresh_from_db()
    assert PASSWORD_PARAM not in operation.params
    assert len(folder().archives()) == 1
    request = (update_dir / "request" / "update").read_text()
    assert request == f"v1.1.0 {operation.params[RUN_PARAM]}\n"


def test_a_retry_without_the_password_asks_nothing(released: Release, update_dir: Path) -> None:
    operation = start()
    operation.params.pop(PASSWORD_PARAM)
    operation.save()

    with pytest.raises(PanelArchiveError):
        update_panel(OperationContext(operation))

    assert not (update_dir / "request" / "update").exists()


def admin_client(client: Client) -> Client:
    client.force_login(UserFactory(role=Role.ADMIN))
    return client


def test_the_page_shows_versions_and_starts_the_update(
    client: Client, released: Release, update_dir: Path
) -> None:
    admin_client(client)
    url = reverse("updates:update")

    page = client.get(url).content.decode()
    assert "1.1.0" in page and "CHANGELOG.md" in page
    assert 'name="password"' in page

    form = {"version": "1.1.0", "password": PASSWORD, "confirmation": PASSWORD}
    response = client.post(url, form)

    assert response.status_code == 302
    assert Operation.objects.filter(kind=UPDATE_KIND).count() == 1
    assert client.get(reverse("updates:status")).status_code == 200


def test_without_the_host_unit_the_page_only_tells(
    client: Client, released: Release, settings: Any, tmp_path: Path
) -> None:
    settings.UPDATE_DIR = tmp_path / "update"
    admin_client(client)

    page = client.get(reverse("updates:update")).content.decode()

    assert "1.1.0" in page
    assert 'name="password"' not in page
    assert "install.sh" in page


def test_the_page_is_for_administrators_only(client: Client) -> None:
    client.force_login(UserFactory(role=Role.MODERATOR))

    assert client.get(reverse("updates:update")).status_code == 403
    assert client.post(reverse("updates:check")).status_code == 403


def test_administrators_see_the_new_release_on_the_dashboard(released: Release) -> None:
    admin = Attention(UserFactory(role=Role.ADMIN))
    moderator = Attention(UserFactory(role=Role.MODERATOR))

    assert any(notice.key == "panel-release:1.1.0" for notice in admin.notices())
    assert moderator.panel_release is None


def test_a_turned_off_check_shows_nothing(released: Release, settings: Any) -> None:
    settings.UPDATE_CHECK = False

    assert Attention(UserFactory(role=Role.ADMIN)).panel_release is None
