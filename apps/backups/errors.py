"""Errors of restoring a protocol on a server, with a message and a hint."""

from enum import StrEnum

from django.utils.translation import gettext_lazy as _

from apps.ssh.errors import CatalogError, ErrorDescription


class RestoreErrorCode(StrEnum):
    OTHER_SERVER_KEY = "other_server_key"
    OTHER_CONTAINER = "other_container"
    NO_BACKUP = "no_backup"
    BACKUP_INVALID = "backup_invalid"
    BACKUP_OF_OTHER_SERVER = "backup_of_other_server"


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
}


class RestoreError(CatalogError[RestoreErrorCode]):
    """Restoring a protocol failed."""

    catalog = RESTORE_ERRORS
