"""Errors of restoring a protocol and of the panel archive, each with a message and a hint."""

from enum import StrEnum

from django.utils.translation import gettext_lazy as _

from apps.ssh.errors import CatalogError, ErrorDescription


class RestoreErrorCode(StrEnum):
    OTHER_SERVER_KEY = "other_server_key"
    OTHER_CONTAINER = "other_container"
    NO_BACKUP = "no_backup"
    BACKUP_INVALID = "backup_invalid"
    BACKUP_OF_OTHER_SERVER = "backup_of_other_server"
    IN_GROUP = "in_group"
    OTHER_PORT = "other_port"
    NOT_RESTORED = "not_restored"


RESTORE_ERRORS: dict[RestoreErrorCode, ErrorDescription] = {
    RestoreErrorCode.OTHER_SERVER_KEY: ErrorDescription(
        _("Another AmneziaWG 2.0 server with other keys already runs on this server."),
        _(
            "The panel does not overwrite it. Remove the amnezia-awg2 container on the server "
            "or restore to another server."
        ),
    ),
    RestoreErrorCode.OTHER_CONTAINER: ErrorDescription(
        _("The container of this protocol on the server has other keys."),
        _(
            "The panel does not overwrite it. Remove the container on the server "
            "or restore to another server."
        ),
    ),
    RestoreErrorCode.NO_BACKUP: ErrorDescription(
        _("The panel has no copy of the files of this protocol."),
        _(
            "Without the copy the old keys cannot work again. Install the protocol anew "
            "and issue new keys."
        ),
    ),
    RestoreErrorCode.BACKUP_INVALID: ErrorDescription(
        _("The saved copy of the protocol files cannot be read."),
        _("Install the protocol anew and issue new keys."),
    ),
    RestoreErrorCode.BACKUP_OF_OTHER_SERVER: ErrorDescription(
        _("The saved copy belongs to other server keys than the panel has for the protocol."),
        _("Import the protocol from the server again, or install it anew and issue new keys."),
    ),
    RestoreErrorCode.IN_GROUP: ErrorDescription(
        _("The server is in a server group: its files are the files of the group."),
        _("Take the server out of the group first, then restore the snapshot."),
    ),
    RestoreErrorCode.OTHER_PORT: ErrorDescription(
        _("The snapshot listens on another port than the protocol now."),
        _("Change the port of the protocol back in its settings, then restore the snapshot."),
    ),
    RestoreErrorCode.NOT_RESTORED: ErrorDescription(
        _("The server files differ from the snapshot after writing them."),
        _("The Amnezia app may have changed the server meanwhile. Run the restore again."),
    ),
}


class RestoreError(CatalogError[RestoreErrorCode]):
    """Restoring a protocol failed."""

    catalog = RESTORE_ERRORS


class PanelArchiveErrorCode(StrEnum):
    NOT_POSTGRES = "not_postgres"
    DUMP_FAILED = "dump_failed"
    NO_PASSWORD = "no_password"
    WRONG_PASSWORD = "wrong_password"
    SHORT_PASSWORD = "short_password"
    REMOTE_INVALID = "remote_invalid"
    REMOTE_UNREACHABLE = "remote_unreachable"
    REMOTE_LOGIN = "remote_login"
    REMOTE_HOST_KEY = "remote_host_key"
    REMOTE_REFUSED = "remote_refused"


PANEL_ARCHIVE_ERRORS: dict[PanelArchiveErrorCode, ErrorDescription] = {
    PanelArchiveErrorCode.NOT_POSTGRES: ErrorDescription(
        _("The panel archive needs the PostgreSQL database of docker compose."),
        _("Install the panel with install.sh or compose.yaml to make archives."),
    ),
    PanelArchiveErrorCode.DUMP_FAILED: ErrorDescription(
        _("The database could not be saved."),
        _("Check that the postgres service runs: docker compose ps. Then make the archive again."),
    ),
    PanelArchiveErrorCode.NO_PASSWORD: ErrorDescription(
        _("The archive password is not kept after the first attempt."),
        _("Make a new archive and enter the password again."),
    ),
    PanelArchiveErrorCode.WRONG_PASSWORD: ErrorDescription(
        _("The archive cannot be read with this password."),
        _("Check the password. A damaged or incomplete file does not open either."),
    ),
    PanelArchiveErrorCode.SHORT_PASSWORD: ErrorDescription(
        _("The password of scheduled archives is too short."),
        _(
            "Set LAMNEZIA_PANEL_ARCHIVE_PASSWORD of at least 12 characters in .env and restart "
            "the panel."
        ),
    ),
    PanelArchiveErrorCode.REMOTE_INVALID: ErrorDescription(
        _("The external storage of the archives is set wrong."),
        _(
            "Check LAMNEZIA_PANEL_ARCHIVE_REMOTE in .env: s3://bucket/folder or "
            "sftp://user@host:port/folder, and the other variables of its kind."
        ),
    ),
    PanelArchiveErrorCode.REMOTE_UNREACHABLE: ErrorDescription(
        _("The external storage did not answer."),
        _(
            "Check its address and that the panel reaches it. The archive stays on the panel: "
            "download it or make a new one once the storage works."
        ),
    ),
    PanelArchiveErrorCode.REMOTE_LOGIN: ErrorDescription(
        _("The external storage refused the login."),
        _("Check the user and the password or the keys of the storage in .env."),
    ),
    PanelArchiveErrorCode.REMOTE_HOST_KEY: ErrorDescription(
        _("The key of the SFTP server differs from LAMNEZIA_PANEL_ARCHIVE_REMOTE_HOST_KEY."),
        _(
            "The step log shows the key the server presented. Check it with ssh-keyscan on a "
            "machine you trust and put it into .env."
        ),
    ),
    PanelArchiveErrorCode.REMOTE_REFUSED: ErrorDescription(
        _("The external storage did not take the archive."),
        _(
            "The step log shows its answer. Check the rights to the folder or the bucket and "
            "the free space. The archive stays on the panel."
        ),
    ),
}


class PanelArchiveError(CatalogError[PanelArchiveErrorCode]):
    """Making or reading a panel archive failed."""

    catalog = PANEL_ARCHIVE_ERRORS
