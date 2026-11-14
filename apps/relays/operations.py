"""Operation kinds of the relays; registered in `RelaysConfig.ready()`."""

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.operations.services.context import OperationContext
from apps.operations.services.registry import operations
from apps.relays.models import Relay
from apps.relays.services.deployment import (
    INSTALL_KIND,
    REMOVE_KIND,
    UPDATE_KIND,
    RelayDeployment,
)


@operations.register(INSTALL_KIND, gettext_lazy("Install the relay"))
def install_relay(ctx: OperationContext) -> None:
    deployment = _deployment(ctx)
    if deployment is not None:
        deployment.install()


@operations.register(UPDATE_KIND, gettext_lazy("Update the relay"))
def update_relay(ctx: OperationContext) -> None:
    deployment = _deployment(ctx)
    if deployment is not None:
        deployment.update()


@operations.register(REMOVE_KIND, gettext_lazy("Remove the relay"))
def remove_relay(ctx: OperationContext) -> None:
    deployment = _deployment(ctx)
    if deployment is not None:
        deployment.remove()


def _deployment(ctx: OperationContext) -> RelayDeployment | None:
    """The relay of the server; a retry after a removal finds none and does nothing."""
    with ctx.step(_("Find the relay")):
        relay = Relay.objects.select_related("server").filter(server_id=ctx.params["server_id"])
        found = relay.first()
        if found is None:
            ctx.log(_("The server is no longer a relay: nothing to do."))
    return None if found is None else RelayDeployment(found, ctx)
