"""The `panel_archive` command: make an encrypted archive of the panel, check or restore one."""

import sys
from argparse import ArgumentParser
from getpass import getpass
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.template.defaultfilters import filesizeformat

from apps.backups.errors import PanelArchiveError
from apps.backups.services.archive_cipher import MIN_PASSWORD_LENGTH
from apps.backups.services.panel_archive import PanelArchiveReader
from apps.backups.services.panel_archives import PanelArchiveFolder
from apps.backups.services.panel_restore import start_panel_restore

_CHUNK = 1024 * 1024


class Command(BaseCommand):
    help = (
        "Make an encrypted archive of the panel (database, .env, uploaded files) in the archive "
        "folder, check that an archive opens with its password, or restore the panel from one."
    )
    stealth_options = ("stdin",)

    def add_arguments(self, parser: ArgumentParser) -> None:
        actions = parser.add_subparsers(dest="action", required=True)
        create = actions.add_parser("create", help="Make an archive now.")
        verify = actions.add_parser("verify", help="Check an archive: password and checksums.")
        verify.add_argument("file", type=Path)
        restore = actions.add_parser(
            "restore", help="Replace the data of this panel with an archive, in the worker."
        )
        restore.add_argument("file", type=Path)
        for action in (create, verify, restore):
            action.add_argument(
                "--password-stdin",
                action="store_true",
                help="Read the password from standard input, for scripts.",
            )

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            if options["action"] == "create":
                self._create(self._password(options, twice=True))
            elif options["action"] == "verify":
                self._verify(options["file"], self._password(options, twice=False))
            else:
                self._restore(options["file"], self._password(options, twice=False))
        except PanelArchiveError as error:
            raise CommandError(f"{error.message} {error.hint}") from error

    def _create(self, password: str) -> None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise CommandError(f"The password needs at least {MIN_PASSWORD_LENGTH} characters.")
        archive, removed = PanelArchiveFolder.from_settings().create(password)
        self.stdout.write(f"Archive {archive.path} ({filesizeformat(archive.size)}).")
        if removed:
            self.stdout.write(f"Old archives removed: {removed}.")

    def _verify(self, file: Path, password: str) -> None:
        manifest = PanelArchiveReader(password).check(file)
        self.stdout.write(
            f"The archive is whole: panel {manifest.panel_version}, "
            f"made {manifest.created_at:%Y-%m-%d %H:%M} UTC."
        )

    def _restore(self, file: Path, password: str) -> None:
        """Checked here first, so a wrong password fails at once; the worker restores it."""
        self._verify(file, password)
        with file.open("rb") as source:
            chunks = iter(lambda: source.read(_CHUNK), b"")
            name = PanelArchiveFolder.from_settings().upload(chunks)
        operation = start_panel_restore(name, password, None, None)
        self.stdout.write(
            f"The restore is operation {operation.pk}: follow it on the Operations page. "
            "Afterwards everyone signs in again."
        )

    def _password(self, options: dict[str, Any], *, twice: bool) -> str:
        if options["password_stdin"]:
            return str((options.get("stdin") or sys.stdin).readline().rstrip("\r\n"))
        password = getpass("Archive password: ")
        if twice and password != getpass("Archive password (again): "):
            raise CommandError("The passwords do not match.")
        return password
