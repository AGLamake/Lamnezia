"""Restoring a panel archive in a running panel: checks, a safety archive, the database, files.

The panel has no access to Docker, so its .env stays as it is: the secrets of the archive are
re-encrypted with the key of this panel, and the settings that differ are only named in the log.
While the database is replaced, other jobs wait and changes from the web get 503 (`RestoreGate`).
"""

import shutil
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from pathlib import Path

from django.conf import settings
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.core.management import call_command
from django.db import models, transaction
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.accounts.models import User, UserSession
from apps.audit.services import journal as audit
from apps.backups.errors import PanelArchiveError, PanelArchiveErrorCode
from apps.backups.services.database_dump import DatabaseLoad
from apps.backups.services.panel_archive import (
    DATABASE,
    ENV,
    MEDIA,
    MIGRATIONS,
    PanelArchiveReader,
    archived_variable,
    newer_migrations,
    read_env,
)
from apps.backups.services.panel_archives import PASSWORD_PARAM, ArchiveFile, PanelArchiveFolder
from apps.core.services.site_settings import cached_settings
from apps.notifications.events import Event
from apps.notifications.services.delivery import emit
from apps.operations.models import Operation, OperationStatus
from apps.operations.services.context import OperationContext
from apps.operations.services.launcher import launch
from apps.operations.services.stale import fail_if_unfinished
from apps.ssh.encryption import SecretCipher, decrypting_also
from apps.ssh.services.rotation import SecretRotation

PANEL_RESTORE_KIND = "backups.restore_panel"
PANEL_RESTORED_ACTION = "backups.panel_restored"
ARCHIVE_PARAM = "archive"
SOURCE_PARAM = "source"
LABEL_PARAM = "label"
_LABEL_LIMIT = 120
ENCRYPTION_VARIABLE = "LAMNEZIA_ENCRYPTION_KEY"
# Settings of this installation rather than of the panel: they stay and are not reported.
MACHINE_VARIABLES = frozenset(
    {
        "LAMNEZIA_DOMAIN",
        "LAMNEZIA_HTTP_BIND",
        "LAMNEZIA_IMAGE",
        "LAMNEZIA_SECRET_KEY",
        ENCRYPTION_VARIABLE,
        "COMPOSE_PROFILES",
        "COMPOSE_PROJECT_NAME",
    }
)
_GATE_KEY = "backups:panel-restoring"
_ACTIVE = (OperationStatus.PENDING, OperationStatus.RUNNING)


class RestoreSource(models.TextChoices):
    """Where a restore was started from, for the audit journal."""

    PAGE = "page", gettext_lazy("the Panel archive page")
    UPDATE_ROLLBACK = "update_rollback", gettext_lazy("the rollback of an update")
    COMMAND = "command", gettext_lazy("the command on the server")


def log_restore(
    action: str, params: Mapping[str, object], *, actor: User | None, ip: str | None = None
) -> None:
    """Audit a restore with the name of its archive and where it came from."""
    name = str(params.get(LABEL_PARAM) or params.get(ARCHIVE_PARAM, ""))
    source = RestoreSource(str(params.get(SOURCE_PARAM, RestoreSource.PAGE)))
    audit.log(
        action,
        actor=actor,
        ip=ip,
        data={"archive": name, "source": source.value},
        summary=f"{name} · {source.label}",
    )


def panel_restoring() -> bool:
    """Whether a restore is replacing the database now."""
    return bool(cache.get(_GATE_KEY))


@contextmanager
def restore_gate() -> Iterator[None]:
    """Mark the panel as being restored for the block; the mark expires with a dead worker."""
    cache.set(_GATE_KEY, True, timeout=settings.OPERATION_TIMEOUT)
    try:
        yield
    finally:
        cache.delete(_GATE_KEY)


def changed_settings(archived: Mapping[str, str], current: Mapping[str, str]) -> list[str]:
    """Names of the panel settings the archive holds with other values than this .env."""
    names = {name for name in (*archived, *current) if archived_variable(name)}
    return sorted(
        name
        for name in names - MACHINE_VARIABLES
        if archived.get(name, "") != current.get(name, "")
    )


def replace_media(source: Path, root: Path) -> None:
    """Put the uploaded files of an archive in place of the current ones."""
    root.mkdir(parents=True, exist_ok=True)
    for entry in root.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    if source.is_dir():
        shutil.copytree(source, root, dirs_exist_ok=True)


def active_restore_operation() -> Operation | None:
    return (
        Operation.objects.filter(kind=PANEL_RESTORE_KIND, status__in=_ACTIVE)
        .order_by("-created_at")
        .first()
    )


def start_panel_restore(  # noqa: PLR0913 - where it came from is keyword-only
    archive: str,
    password: str,
    user: User | None,
    ip: str | None,
    *,
    source: RestoreSource,
    label: str = "",
) -> Operation:
    """Queue the restore of an archive in the folder; the password travels encrypted.

    `label` is the name the journal shows, e.g. of the uploaded file, when it differs from the
    name in the folder. While another restore waits or runs, that one is returned instead.
    """
    with transaction.atomic():
        active = active_restore_operation()
        if active is not None:
            return active
        token = SecretCipher.from_settings().encrypt(password)
        params = {
            ARCHIVE_PARAM: archive,
            PASSWORD_PARAM: token,
            SOURCE_PARAM: source.value,
            LABEL_PARAM: (label or archive)[:_LABEL_LIMIT],
        }
        operation = launch(PANEL_RESTORE_KIND, params, user=user)
        log_restore(PANEL_RESTORE_KIND, params, actor=user, ip=ip)
    return operation


class PanelRestore:
    """Replaces the data of this panel with an archive, or leaves the panel as it was.

    Wrong password, a damaged file or an archive of a newer panel stop it before anything
    changes. Then a safety archive of the current state is made with the same password; if the
    restored data cannot be brought up to this version, the safety archive is put back.
    """

    def __init__(
        self,
        ctx: OperationContext,
        password: str,
        folder: PanelArchiveFolder,
        load: DatabaseLoad,
        environ: Mapping[str, str],
    ) -> None:
        self._ctx = ctx
        self._reader = PanelArchiveReader(password)
        self._password = password
        self._folder = folder
        self._load = load
        self._environ = environ

    def run(self, archive: Path) -> None:
        with self._unpacked() as work:
            env = self._check(archive, work)
            with restore_gate():
                self._ensure_idle()
                safety = self._save_safety()
                self._load_database(work, _("Load the database of the archive"))
                try:
                    self._settle(work, env)
                except Exception as exc:
                    self._roll_back(safety, exc)
                    raise PanelArchiveError(PanelArchiveErrorCode.ROLLED_BACK, repr(exc)) from exc

    @contextmanager
    def _unpacked(self) -> Iterator[Path]:
        """A folder for the unpacked archive; it holds the unencrypted dump, so it always goes."""
        work = self._folder.work_dir()
        try:
            yield work
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _check(self, archive: Path, work: Path) -> dict[str, str]:
        with self._ctx.step(_("Check the password, the file and the version of the archive")):
            manifest = self._reader.unpack(archive, work)
            self._ctx.log(
                _("Panel %(version)s, made %(time)s UTC.")
                % {
                    "version": manifest.panel_version,
                    "time": f"{manifest.created_at:%Y-%m-%d %H:%M}",
                }
            )
            newer = newer_migrations((work / MIGRATIONS).read_text())
            if newer:
                self._ctx.log(_("This version lacks %(file)s.") % {"file": newer[0]})
                raise PanelArchiveError(PanelArchiveErrorCode.ARCHIVE_NEWER, newer[0])
            return read_env((work / ENV).read_text())

    def _ensure_idle(self) -> None:
        """Other jobs would write into the database being replaced, or into the new one."""
        others = Operation.objects.filter(status__in=_ACTIVE).exclude(pk=self._ctx.operation_id)
        if others.exists():
            raise PanelArchiveError(PanelArchiveErrorCode.PANEL_BUSY)

    def _save_safety(self) -> ArchiveFile:
        with self._ctx.step(_("Save the current state into a safety archive")):
            safety, _removed = self._folder.create(self._password, prune=False)
            self._ctx.log(
                _("Safety archive %(name)s, with the password of the restored one.")
                % {"name": safety.name}
            )
        return safety

    def _load_database(self, work: Path, title: str) -> None:
        """Replace the database; a failure leaves the old one untouched."""
        with self._ctx.step(title):
            try:
                with self._ctx.database_replaced():
                    self._load.load(work / DATABASE)
            except PanelArchiveError as error:
                if error.detail:
                    self._ctx.log(_("Answer: %(detail)s") % {"detail": error.detail})
                raise

    def _settle(self, work: Path, env: Mapping[str, str]) -> None:
        """The files, the schema of this version, the secrets under this key, no old sessions."""
        keys = env.get(ENCRYPTION_VARIABLE, "")
        foreign = keys and keys != settings.ENCRYPTION_KEY
        with self._ctx.step(_("Bring the data up to this panel")):
            replace_media(work / MEDIA, settings.MEDIA_ROOT)
            with decrypting_also(keys) if foreign else nullcontext():
                call_command("migrate", interactive=False, verbosity=0)
                rows = SecretRotation().run()
            self._ctx.log(
                _("Secrets re-encrypted with the key of this panel: %(count)d.") % {"count": rows}
            )
            self._sign_out_everyone()
            self._close_archived_operations()
            cached_settings.invalidate()
            self._report_settings(changed_settings(env, self._environ))

    def _close_archived_operations(self) -> None:
        """Jobs the archive caught under way have no worker here: they fail now, not in hours."""
        others = Operation.objects.filter(status__in=_ACTIVE).exclude(pk=self._ctx.operation_id)
        closed = sum(fail_if_unfinished(pk) for pk in others.values_list("pk", flat=True))
        if closed:
            self._ctx.log(
                _("Operations the archive caught unfinished are closed: %(count)d.")
                % {"count": closed}
            )

    def _report_settings(self, changed: list[str]) -> None:
        """Settings live in .env, out of reach of the panel: the administrators hear of them."""
        if not changed:
            return
        names = ", ".join(changed)
        self._ctx.log(
            _(
                "These settings of the archive differ from the .env of this panel and "
                "stay as they are: %(names)s. Copy them into .env if needed and restart "
                "the panel."
            )
            % {"names": names}
        )
        emit(Event.PANEL_SETTINGS_DIFFER, {"names": names})

    def _sign_out_everyone(self) -> None:
        """Sessions of the archive could be ones revoked since; everyone signs in again."""
        Session.objects.all().delete()
        UserSession.objects.all().delete()
        self._ctx.log(_("Everyone signs in again, with the accounts of the archive."))

    def _roll_back(self, safety: ArchiveFile, cause: Exception) -> None:
        with self._unpacked() as work:
            self._reader.unpack(safety.path, work)
            self._load_database(work, _("Put back the state from the safety archive"))
            with self._ctx.step(_("Put back the files from the safety archive")):
                self._ctx.log(_("The restore failed: %(error)s") % {"error": cause})
                replace_media(work / MEDIA, settings.MEDIA_ROOT)
                cached_settings.invalidate()
