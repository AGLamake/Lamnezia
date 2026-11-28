"""Errors of moving the panel to another machine, each with a message and a hint."""

from enum import StrEnum

from django.utils.translation import gettext_lazy as _

from apps.ssh.errors import CatalogError, ErrorDescription


class MoveErrorCode(StrEnum):
    BUSY = "busy"
    WRONG_STATE = "wrong_state"
    UNSUPPORTED_SYSTEM = "unsupported_system"
    TARGET_IN_USE = "target_in_use"
    SAME_MACHINE = "same_machine"
    LOW_DISK = "low_disk"
    LOW_MEMORY = "low_memory"
    PORTS_BUSY = "ports_busy"
    UPLOAD_FAILED = "upload_failed"
    INSTALL_FAILED = "install_failed"
    NOT_STARTED = "not_started"
    RELAY_BROKEN = "relay_broken"
    ARRIVED = "arrived"
    NOT_DISCARDED = "not_discarded"
    PARTLY_CONFIRMED = "partly_confirmed"
    INTERRUPTED = "move_interrupted"


MOVE_ERRORS: dict[MoveErrorCode, ErrorDescription] = {
    MoveErrorCode.BUSY: ErrorDescription(
        _("Other operations run on the panel."),
        _("Wait until they end (Operations), then start the move again."),
    ),
    MoveErrorCode.WRONG_STATE: ErrorDescription(
        _("The move is not in the state this step needs."),
        _("Open System → Panel move to see what can be done now."),
    ),
    MoveErrorCode.UNSUPPORTED_SYSTEM: ErrorDescription(
        _("The new machine does not run Debian or Ubuntu."),
        _("The panel installer supports Debian and Ubuntu only. Choose another machine."),
    ),
    MoveErrorCode.TARGET_IN_USE: ErrorDescription(
        _("The new machine already has a panel or its nginx site."),
        _(
            "The move installs the panel only on a machine without one. Remove /opt/lamnezia "
            "and /etc/nginx/sites-available/lamnezia there, or choose another machine."
        ),
    ),
    MoveErrorCode.SAME_MACHINE: ErrorDescription(
        _("The new machine is the machine this panel runs on."),
        _(
            "The panel connects to it from its own address. Choose another machine: the move "
            "installs the panel there."
        ),
    ),
    MoveErrorCode.LOW_DISK: ErrorDescription(
        _("The new machine has too little free disk space."),
        _(
            "The panel needs 3 GB free on / for its images and twice its database and files on "
            "top: for the archive and the restored copy. Free some space or choose a machine "
            "with a larger disk."
        ),
    ),
    MoveErrorCode.LOW_MEMORY: ErrorDescription(
        _("The new machine has too little memory."),
        _(
            "The panel needs about 1 GB of memory, swap included. Add a swap file there or "
            "choose a machine with more memory."
        ),
    ),
    MoveErrorCode.PORTS_BUSY: ErrorDescription(
        _("Ports 80 and 443 of the new machine are taken, and there is no nginx on it."),
        _(
            "The panel needs these ports for its web server. Free them, or install nginx on "
            "the machine: then the panel adds a site to it."
        ),
    ),
    MoveErrorCode.UPLOAD_FAILED: ErrorDescription(
        _("The files of the panel did not reach the new machine intact."),
        _("Check the free space and the connection, then start the move again."),
    ),
    MoveErrorCode.INSTALL_FAILED: ErrorDescription(
        _("The installer failed on the new machine."),
        _("The log of the step shows the reason. Fix it and start the move again."),
    ),
    MoveErrorCode.NOT_STARTED: ErrorDescription(
        _("The copy on the new machine does not answer as expected."),
        _("Look at its logs there: cd /opt/lamnezia && docker compose logs web."),
    ),
    MoveErrorCode.RELAY_BROKEN: ErrorDescription(
        _("The relay on the new machine does not run with its ports after the install."),
        _("Open the relay of the server and update it, then start the move again."),
    ),
    MoveErrorCode.ARRIVED: ErrorDescription(
        _("The copy on the new machine already accepts changes."),
        _(
            "It is the panel now: it is not removed. Point the domain to it, or confirm the move "
            "again to make this copy read-only."
        ),
    ),
    MoveErrorCode.NOT_DISCARDED: ErrorDescription(
        _("The copy on the new machine could not be removed."),
        _(
            "Remove it on the new machine by hand: cd /opt/lamnezia && docker compose down -v, "
            "then delete /opt/lamnezia and the nginx site lamnezia, if there is one."
        ),
    ),
    MoveErrorCode.PARTLY_CONFIRMED: ErrorDescription(
        _("The confirmation has started, and the copy on the new machine could not be removed."),
        _(
            "Retry the confirmation once the new machine answers, or cancel the move without the "
            "new machine on System → Panel move."
        ),
    ),
    MoveErrorCode.INTERRUPTED: ErrorDescription(
        _("The preparation stopped answering for too long, and the move was failed."),
        _(
            "The panel accepts changes again. Start the move once more: the copy left on the new "
            "machine is removed first."
        ),
    ),
}


class MoveError(CatalogError[MoveErrorCode]):
    """A step of the panel move failed."""

    catalog = MOVE_ERRORS
