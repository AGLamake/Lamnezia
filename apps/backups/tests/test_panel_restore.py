import io
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.audit.models import AuditEvent
from apps.backups.errors import PanelArchiveError, PanelArchiveErrorCode
from apps.backups.operations import restore_panel
from apps.backups.services import panel_archive, panel_restore
from apps.backups.services.database_dump import PostgresLoad
from apps.backups.services.panel_archive import (
    PanelArchiveReader,
    PanelArchiveWriter,
    env_file,
    newer_migrations,
    read_env,
)
from apps.backups.services.panel_archives import PASSWORD_PARAM, UPLOAD_PREFIX
from apps.backups.services.panel_restore import (
    ARCHIVE_PARAM,
    PANEL_RESTORE_KIND,
    PANEL_RESTORED_ACTION,
    PanelRestore,
    RestoreSource,
    changed_settings,
    panel_restoring,
    replace_media,
    restore_gate,
    start_panel_restore,
)
from apps.backups.tests.panel_archive_helpers import PASSWORD, FakeDump, folder
from apps.notifications.events import Event
from apps.notifications.models import Notification
from apps.notifications.tests.factories import ChannelFactory
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.context import OperationContext
from apps.relocation.services.freeze import RESTORING_ERROR, TaskGate
from apps.servers.models import Server
from apps.servers.tests.factories import ServerFactory
from apps.ssh.encryption import SecretCipher

pytestmark = pytest.mark.django_db


class FakeLoad:
    """pg_restore without PostgreSQL: remembers the dumps and runs `effect` as the new data."""

    def __init__(self, effect: Callable[[], object] = lambda: None, fail: bool = False) -> None:
        self.effect = effect
        self.fail = fail
        self.loaded: list[bytes] = []
        self.restoring: list[bool] = []

    def load(self, path: Path) -> None:
        self.loaded.append(path.read_bytes())
        self.restoring.append(panel_restoring())
        if self.fail:
            raise PanelArchiveError(PanelArchiveErrorCode.LOAD_FAILED, "ERROR: relation exists")
        self.effect()


def make_archive(target: Path, environ: dict[str, str] | None = None) -> Path:
    PanelArchiveWriter(PASSWORD, FakeDump(), environ or {}, settings.MEDIA_ROOT).write(target)
    return target


def running_operation(user: User | None = None) -> Operation:
    return Operation.objects.create(
        kind=PANEL_RESTORE_KIND, status=OperationStatus.RUNNING, created_by=user
    )


def restore(
    operation: Operation,
    load: FakeLoad,
    password: str = PASSWORD,
    environ: dict[str, str] | None = None,
) -> PanelRestore:
    return PanelRestore(OperationContext(operation), password, folder(), load, environ or {})


def logs(operation: Operation) -> str:
    return "\n".join(step.log for step in operation.steps.all())


def media_files() -> set[str]:
    root = Path(settings.MEDIA_ROOT)
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


# The archived .env and the settings


def test_the_archived_env_reads_back_unchanged() -> None:
    environ = {
        "LAMNEZIA_PLAIN": "a.b:c/d",
        "LAMNEZIA_SPACED": "two words $HOME",
        "LAMNEZIA_HARD": 'it\'s "quoted" \\ $x\nsecond line',
        "LAMNEZIA_EMPTY": "",
    }

    assert read_env(env_file(environ)) == environ


def test_only_panel_settings_that_differ_are_named() -> None:
    archived = {"LAMNEZIA_SITE_NAME": "Old", "LAMNEZIA_DOMAIN": "old.example", "LAMNEZIA_X": "1"}
    current = {"LAMNEZIA_SITE_NAME": "New", "LAMNEZIA_DOMAIN": "new.example", "PATH": "/bin"}

    assert changed_settings(archived, current) == ["LAMNEZIA_SITE_NAME", "LAMNEZIA_X"]


def test_migrations_missing_from_the_code_mean_a_newer_archive() -> None:
    listed = "apps/backups/migrations/0001_initial.py\napps/backups/migrations/9999_future.py\n"

    assert newer_migrations(listed) == ["apps/backups/migrations/9999_future.py"]


def test_the_media_of_the_archive_replace_the_current_ones(tmp_path: Path) -> None:
    source, root = tmp_path / "source", tmp_path / "root"
    (source / "logos").mkdir(parents=True)
    (source / "logos" / "logo.png").write_bytes(b"old")
    (root / "extra").mkdir(parents=True)
    (root / "extra" / "new.png").write_bytes(b"new")
    (root / "top.txt").write_text("x")

    replace_media(source, root)

    assert [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()] == [
        "logos/logo.png"
    ]


# Unpacking


def test_unpack_gives_the_files_of_the_archive(archive_settings: Path, tmp_path: Path) -> None:
    archive = make_archive(tmp_path / "panel.lmz", {"LAMNEZIA_SITE_NAME": "Old"})
    target = tmp_path / "work"
    target.mkdir()

    PanelArchiveReader(PASSWORD).unpack(archive, target)

    assert (target / "database.dump").read_bytes() == FakeDump().content
    assert read_env((target / "env").read_text()) == {"LAMNEZIA_SITE_NAME": "Old"}
    assert (target / "media" / "logos" / "logo.png").read_bytes() == b"png"


@pytest.mark.parametrize(
    ("password", "damage", "code"),
    [
        ("wrong password", False, PanelArchiveErrorCode.WRONG_PASSWORD),
        (PASSWORD, True, PanelArchiveErrorCode.DAMAGED),
    ],
)
def test_unpack_refuses_a_wrong_password_or_a_damaged_file(
    archive_settings: Path, tmp_path: Path, password: str, damage: bool, code: str
) -> None:
    archive = make_archive(tmp_path / "panel.lmz")
    if damage:
        raw = bytearray(archive.read_bytes())
        raw[len(raw) // 2] ^= 0xFF
        archive.write_bytes(bytes(raw))
    target = tmp_path / "work"
    target.mkdir()

    with pytest.raises(PanelArchiveError) as error:
        PanelArchiveReader(password).unpack(archive, target)

    assert error.value.code == code
    assert not (target / "database.dump").exists()


# The restore


def test_a_restore_saves_the_current_state_and_brings_in_the_archive(
    archive_settings: Path, fake_dump: FakeDump, tmp_path: Path
) -> None:
    archive = make_archive(
        tmp_path / "panel.lmz", {"LAMNEZIA_SITE_NAME": "Old", "LAMNEZIA_DOMAIN": "old.example"}
    )
    media = Path(settings.MEDIA_ROOT)
    (media / "logos" / "logo.png").unlink()
    (media / "later.png").write_bytes(b"later")
    admin = UserFactory(role=Role.ADMIN, username="admin")
    Session.objects.create(
        session_key="s", session_data="", expire_date=timezone.now() + timedelta(days=1)
    )
    operation = running_operation(admin)

    def archived_database() -> None:
        Operation.objects.all().delete()
        User.objects.all().delete()
        UserFactory(role=Role.ADMIN, username="admin")

    load = FakeLoad(archived_database)
    environ = {"LAMNEZIA_SITE_NAME": "New", "LAMNEZIA_DOMAIN": "new.example"}

    restore(operation, load, environ=environ).run(archive)

    assert load.loaded == [FakeDump().content]
    assert load.restoring == [True]
    assert not panel_restoring()
    assert media_files() == {"logos/logo.png"}
    assert not Session.objects.exists()
    (safety,) = folder().archives()
    assert safety.name in logs(operation)
    saved = Operation.objects.get(pk=operation.pk)
    assert saved.created_by == User.objects.get(username="admin")
    assert saved.steps.count() == 4
    assert "LAMNEZIA_SITE_NAME" in logs(saved)
    assert "LAMNEZIA_DOMAIN" not in logs(saved)
    assert not list(Path(settings.PANEL_ARCHIVE_DIR).glob(".dump-*"))


def test_operations_the_archive_caught_unfinished_are_closed_at_once(
    archive_settings: Path, fake_dump: FakeDump, tmp_path: Path
) -> None:
    archive = make_archive(tmp_path / "panel.lmz")
    operation = running_operation()

    def archived_database() -> None:
        Operation.objects.create(kind="backups.panel_archive", status=OperationStatus.RUNNING)
        Operation.objects.create(kind="keys.issue")

    restore(operation, FakeLoad(archived_database)).run(archive)

    others = Operation.objects.exclude(pk=operation.pk)
    assert {other.status for other in others} == {OperationStatus.FAILED}
    assert "closed: 2" in logs(operation)


def test_settings_that_differ_ask_the_staff_to_restart_the_panel(
    archive_settings: Path,
    fake_dump: FakeDump,
    tmp_path: Path,
    django_capture_on_commit_callbacks: Any,
) -> None:
    ChannelFactory(events=[Event.PANEL_SETTINGS_DIFFER])
    archive = make_archive(tmp_path / "panel.lmz", {"LAMNEZIA_SITE_NAME": "Old"})

    with django_capture_on_commit_callbacks(execute=True):
        for environ in ({"LAMNEZIA_SITE_NAME": "Old"}, {}):
            operation = running_operation()
            restore(operation, FakeLoad(), environ=environ).run(archive)
            Operation.objects.filter(pk=operation.pk).update(status=OperationStatus.SUCCEEDED)

    (notice,) = Notification.objects.all()
    assert notice.event == Event.PANEL_SETTINGS_DIFFER
    assert notice.data == {"names": "LAMNEZIA_SITE_NAME"}


def test_secrets_of_another_key_are_re_encrypted_with_this_one(
    archive_settings: Path, fake_dump: FakeDump, tmp_path: Path
) -> None:
    archive_key, panel_key = Fernet.generate_key().decode(), settings.ENCRYPTION_KEY
    settings.ENCRYPTION_KEY = archive_key
    server = ServerFactory(password="ssh-password")
    archive = make_archive(tmp_path / "panel.lmz", {"LAMNEZIA_ENCRYPTION_KEY": archive_key})
    settings.ENCRYPTION_KEY = panel_key

    restore(running_operation(), FakeLoad()).run(archive)

    assert panel_key == settings.ENCRYPTION_KEY
    assert Server.objects.get(pk=server.pk).password == "ssh-password"


def test_the_safety_archive_keeps_the_restored_one_in_the_folder(
    archive_settings: Path, fake_dump: FakeDump
) -> None:
    made, _removed = folder(keep=1).create(PASSWORD)
    oldest = made.path.replace(made.path.with_name("lamnezia-20200101-000000.lamnezia"))
    load = FakeLoad()

    PanelRestore(OperationContext(running_operation()), PASSWORD, folder(keep=1), load, {}).run(
        oldest
    )

    assert oldest.exists()
    assert len(folder().archives()) == 2


@pytest.mark.parametrize("case", ["wrong password", "newer archive", "busy panel"])
def test_a_refused_restore_changes_nothing(
    archive_settings: Path,
    fake_dump: FakeDump,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    password = PASSWORD
    if case == "newer archive":
        future = ["apps/backups/migrations/9999_future.py"]
        monkeypatch.setattr(panel_archive, "migration_files", lambda: future)
    archive = make_archive(tmp_path / "panel.lmz")
    if case == "wrong password":
        password = "wrong password"
    if case == "busy panel":
        Operation.objects.create(kind="keys.issue")
    load = FakeLoad()

    with pytest.raises(PanelArchiveError) as error:
        restore(running_operation(), load, password).run(archive)

    expected = {
        "wrong password": PanelArchiveErrorCode.WRONG_PASSWORD,
        "newer archive": PanelArchiveErrorCode.ARCHIVE_NEWER,
        "busy panel": PanelArchiveErrorCode.PANEL_BUSY,
    }
    assert error.value.code == expected[case]
    assert load.loaded == []
    assert folder().archives() == []
    assert not panel_restoring()


def test_a_failed_load_leaves_the_database_and_logs_the_answer(
    archive_settings: Path, fake_dump: FakeDump, tmp_path: Path
) -> None:
    archive = make_archive(tmp_path / "panel.lmz")
    operation = running_operation()
    load = FakeLoad(fail=True)

    with pytest.raises(PanelArchiveError) as error:
        restore(operation, load).run(archive)

    assert error.value.code == PanelArchiveErrorCode.LOAD_FAILED
    assert len(load.loaded) == 1
    assert "relation exists" in logs(operation)


def test_data_that_cannot_be_brought_up_are_rolled_back(
    archive_settings: Path, fake_dump: FakeDump, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = make_archive(tmp_path / "panel.lmz")
    (Path(settings.MEDIA_ROOT) / "later.png").write_bytes(b"later")

    def broken_migrate(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("migration failed")

    monkeypatch.setattr(panel_restore, "call_command", broken_migrate)
    operation = running_operation()
    load = FakeLoad()

    with pytest.raises(PanelArchiveError) as error:
        restore(operation, load).run(archive)

    assert error.value.code == PanelArchiveErrorCode.ROLLED_BACK
    assert load.loaded == [FakeDump().content, fake_dump.content]
    assert media_files() == {"logos/logo.png", "later.png"}
    assert "migration failed" in logs(operation)
    assert not panel_restoring()


# The operation, the page and the command


def test_the_operation_forgets_the_password_and_removes_the_upload(
    archive_settings: Path, fake_dump: FakeDump, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    load = FakeLoad()
    monkeypatch.setattr(PostgresLoad, "from_settings", staticmethod(lambda: load))
    raw = make_archive(tmp_path / "panel.lmz").read_bytes()
    name = folder().upload([raw])
    operation = start_panel_restore(
        name, PASSWORD, UserFactory(role=Role.ADMIN), None, source=RestoreSource.PAGE
    )
    assert PASSWORD not in str(operation.params)
    operation.status = OperationStatus.RUNNING
    operation.save()

    restore_panel(OperationContext(operation))

    assert PASSWORD_PARAM not in Operation.objects.get(pk=operation.pk).params
    assert not (Path(settings.PANEL_ARCHIVE_DIR) / name).exists()
    assert len(load.loaded) == 1


def test_the_restored_journal_records_the_restore_again(
    archive_settings: Path, fake_dump: FakeDump, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    load = FakeLoad(lambda: AuditEvent.objects.all().delete())
    monkeypatch.setattr(PostgresLoad, "from_settings", staticmethod(lambda: load))
    polls: list[bool] = []
    monkeypatch.setattr("apps.backups.operations.monitor_servers.delay", lambda: polls.append(True))
    folder().create(PASSWORD, FakeDump())
    (archive,) = folder().archives()
    admin = UserFactory(role=Role.ADMIN, username="admin")
    operation = start_panel_restore(
        archive.name, PASSWORD, admin, None, source=RestoreSource.UPDATE_ROLLBACK
    )
    operation.status = OperationStatus.RUNNING
    operation.save()

    restore_panel(OperationContext(operation))

    (event,) = AuditEvent.objects.all()
    assert event.action == PANEL_RESTORED_ACTION
    assert event.actor_name == "admin"
    assert event.data == {"archive": archive.name, "source": "update_rollback"}
    assert polls == [True]


def test_a_missing_archive_fails_the_operation(archive_settings: Path) -> None:
    operation = start_panel_restore(
        f"{UPLOAD_PREFIX}{'0' * 32}", PASSWORD, None, None, source=RestoreSource.COMMAND
    )

    with pytest.raises(PanelArchiveError) as error:
        restore_panel(OperationContext(operation))

    assert error.value.code == PanelArchiveErrorCode.ARCHIVE_GONE


def test_one_restore_runs_at_a_time(archive_settings: Path) -> None:
    first = start_panel_restore("a", PASSWORD, None, None, source=RestoreSource.COMMAND)

    assert start_panel_restore("b", PASSWORD, None, None, source=RestoreSource.COMMAND) == first


def test_the_page_uploads_the_archive_and_starts_the_restore(
    client: Client, archive_settings: Path, tmp_path: Path
) -> None:
    client.force_login(UserFactory(role=Role.ADMIN))
    raw = make_archive(tmp_path / "panel.lmz").read_bytes()

    response = client.post(
        "/backups/panel/restore/",
        {"upload": SimpleUploadedFile("panel.lmz", raw), "restore_password": PASSWORD},
    )

    operation = Operation.objects.get(kind=PANEL_RESTORE_KIND)
    assert response.status_code == 302
    assert response["Location"].endswith(f"/{operation.pk}/")
    name = operation.params[ARCHIVE_PARAM]
    assert name.startswith(UPLOAD_PREFIX)
    assert (Path(settings.PANEL_ARCHIVE_DIR) / name).read_bytes() == raw
    event = AuditEvent.objects.get(action=PANEL_RESTORE_KIND)
    assert event.data == {"archive": "panel.lmz", "source": "page"}
    assert event.target_repr.startswith("panel.lmz · ")


def test_the_page_names_the_upload_limit_and_the_command(
    client: Client, archive_settings: Path, settings: Any
) -> None:
    client.force_login(UserFactory(role=Role.ADMIN))
    settings.PANEL_ARCHIVE_UPLOAD_MB = 20

    page = client.get("/backups/panel/").content.decode()
    assert "up to 20 MB" in page
    assert "panel_archive restore /tmp/panel.lamnezia" in page

    settings.PANEL_ARCHIVE_UPLOAD_MB = 0
    assert "panel_archive restore" not in client.get("/backups/panel/").content.decode()


def test_the_page_wants_an_archive(client: Client, archive_settings: Path) -> None:
    client.force_login(UserFactory(role=Role.ADMIN))

    response = client.post("/backups/panel/restore/", {"restore_password": PASSWORD})

    assert response.status_code == 200
    assert not Operation.objects.filter(kind=PANEL_RESTORE_KIND).exists()


def test_only_admins_restore_the_panel(client: Client, archive_settings: Path) -> None:
    client.force_login(UserFactory(role=Role.MODERATOR))

    response = client.post("/backups/panel/restore/", {"restore_password": PASSWORD})

    assert response.status_code in (302, 403)
    assert not Operation.objects.exists()


def test_the_command_checks_the_archive_and_queues_the_restore(
    archive_settings: Path, tmp_path: Path
) -> None:
    archive = make_archive(tmp_path / "panel.lmz")
    out = io.StringIO()

    call_command(
        "panel_archive", "restore", str(archive), "--password-stdin",
        stdin=io.StringIO(PASSWORD), stdout=out,
    )  # fmt: skip

    operation = Operation.objects.get(kind=PANEL_RESTORE_KIND)
    assert f"operation {operation.pk}" in out.getvalue()
    event = AuditEvent.objects.get(action=PANEL_RESTORE_KIND)
    assert event.data == {"archive": "panel.lmz", "source": "command"}
    assert SecretCipher.from_settings().decrypt(operation.params[PASSWORD_PARAM]) == PASSWORD


# Everything else waits


def test_other_jobs_and_changes_wait_while_the_panel_is_restored(client: Client) -> None:
    client.force_login(UserFactory(role=Role.ADMIN))
    pending = Operation.objects.create(kind="keys.issue")

    with restore_gate():
        assert not TaskGate().allows("operations.run", (pending.pk,))
        assert not TaskGate().allows("monitoring.poll", ())
        change = client.post("/backups/panel/", {"password": "x"})
        page = client.get("/backups/panel/")

    pending.refresh_from_db()
    assert (pending.status, pending.error_code) == (OperationStatus.FAILED, RESTORING_ERROR)
    assert change.status_code == 503
    assert change["Retry-After"]
    assert page.status_code == 200
    assert not panel_restoring()


# pg_restore piped into psql

POSTGRES = {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": "lamnezia",
    "USER": "lamnezia",
    "PASSWORD": "db-password",
    "HOST": "postgres",
    "PORT": "5432",
}
# Stand-ins: pg_restore prints the dump as SQL, psql keeps what it got and may fail.
PG_RESTORE = "import sys; sys.stdout.buffer.write(open(sys.argv[-1], 'rb').read())"
PSQL = (
    "import os, sys; data = sys.stdin.buffer.read();"
    " open(os.environ['FAKE_PSQL_OUT'], 'wb').write(data);"
    " fail = os.environ.get('FAKE_PSQL_FAIL', ''); sys.stderr.write(fail);"
    " sys.exit(3 if fail else 0)"
)


@pytest.fixture
def fake_postgres(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    real = subprocess.Popen
    out = tmp_path / "psql-input.sql"
    monkeypatch.setenv("FAKE_PSQL_OUT", str(out))

    def popen(command: list[str], **kwargs: Any) -> Any:
        script = {"pg_restore": PG_RESTORE, "psql": PSQL}[command[0]]
        if kwargs.get("env"):
            kwargs["env"] = {**os.environ, **kwargs["env"]}
        return real([sys.executable, "-c", script, *command[1:]], **kwargs)

    monkeypatch.setattr("apps.backups.services.database_dump.subprocess.Popen", popen)
    return out


def test_the_load_empties_the_schema_and_runs_the_dump_in_psql(
    fake_postgres: Path, tmp_path: Path
) -> None:
    dump = tmp_path / "database.dump"
    dump.write_bytes(b"CREATE TABLE t ();\n")

    PostgresLoad(POSTGRES).load(dump)

    sql = fake_postgres.read_bytes()
    assert sql.startswith(b"SET client_min_messages = warning;\nDROP SCHEMA public CASCADE;")
    assert sql.endswith(b"CREATE TABLE t ();\n")


def test_a_failed_load_says_what_psql_answered(
    fake_postgres: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_PSQL_FAIL", "ERROR: syntax error")
    dump = tmp_path / "database.dump"
    dump.write_bytes(b"BROKEN;\n")

    with pytest.raises(PanelArchiveError) as error:
        PostgresLoad(POSTGRES).load(dump)

    assert error.value.code == PanelArchiveErrorCode.LOAD_FAILED
    assert "syntax error" in error.value.detail
