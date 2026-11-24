"""Why a panel update cannot start, and what the reasons the host script writes mean."""

from enum import StrEnum

from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from apps.ssh.errors import CatalogError, ErrorDescription


class UpdateErrorCode(StrEnum):
    NO_HOST = "no_host"
    NOT_OFFERED = "not_offered"
    BUSY = "busy"


UPDATE_ERRORS: dict[UpdateErrorCode, ErrorDescription] = {
    UpdateErrorCode.NO_HOST: ErrorDescription(
        _("The host of this panel does not take update requests."),
        _(
            "Run install.sh of the current version on the host once: it installs the "
            "lamnezia-update unit. Or update by hand as the Upgrade page of the wiki says."
        ),
    ),
    UpdateErrorCode.NOT_OFFERED: ErrorDescription(
        _("This version is not the latest release newer than the panel."),
        _("Open the page again: the panel offers only the latest release."),
    ),
    UpdateErrorCode.BUSY: ErrorDescription(
        _("An update is already asked for or running."),
        _("Wait until it ends; its log is on this page."),
    ),
}


class UpdateError(CatalogError[UpdateErrorCode]):
    """A panel update was not started."""

    catalog = UPDATE_ERRORS


# The `reason` of deploy/update/lamnezia-deploy.sh release.
HOST_REASONS: dict[str, Promise] = {
    "bad_request": _("The host could not read the request."),
    "not_git": _("The panel is not installed from git: update it by hand."),
    "fetch_failed": _("The release could not be fetched from GitHub."),
    "not_in_channel": _("The release is not on the branch this installation follows."),
    "not_newer": _("The release is not newer than the running version: nothing was changed."),
    "unverified": _("The release does not match the manifest published with it."),
    "unsigned": _("The release has no signature: it was not published by the release workflow."),
    "bad_signature": _("The signature of the release is not valid for the keys of this version."),
    "pull_failed": _("The image of the release could not be downloaded."),
    "switch_failed": _("The code could not be moved to the release."),
    "build_failed": _("The image could not be built."),
    "start_failed": _("The new version could not be started."),
    "unhealthy": _("The new version did not answer the health check."),
    "rollback_failed": _("Going back to the previous version failed: check the host."),
    "interrupted": _("The update was interrupted."),
}


def host_reason(code: str) -> str:
    """The reason in words; an unknown code is shown as is."""
    reason = HOST_REASONS.get(code)
    return str(reason) if reason is not None else code
