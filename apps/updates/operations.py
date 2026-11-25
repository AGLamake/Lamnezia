"""The panel update operation; registered in `UpdatesConfig.ready()`."""

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.backups.operations import write_panel_archive
from apps.operations.services.context import OperationContext
from apps.operations.services.registry import operations
from apps.updates.errors import UpdateError, UpdateErrorCode
from apps.updates.services.host import HostUpdater
from apps.updates.services.updates import ARCHIVE_PARAM, RUN_PARAM, UPDATE_KIND, VERSION_PARAM
from apps.updates.services.versions import Version


@operations.register(UPDATE_KIND, gettext_lazy("Update the panel"))
def update_panel(ctx: OperationContext) -> None:
    """Archives the panel, then asks the host for the update; the host restarts this worker."""
    version = Version.parse(ctx.params[VERSION_PARAM])
    if version is None:
        raise UpdateError(UpdateErrorCode.NOT_OFFERED, ctx.params[VERSION_PARAM])
    host = HostUpdater.from_settings()
    if not host.available:
        raise UpdateError(UpdateErrorCode.NO_HOST)
    archive = write_panel_archive(ctx)
    ctx.remember(ARCHIVE_PARAM, archive.name)
    with ctx.step(_("Ask the host to update the panel")):
        host.request(version, ctx.params[RUN_PARAM])
        ctx.log(
            _("The host updates the panel to %(version)s; its log is on the Panel update page.")
            % {"version": version.tag}
        )
