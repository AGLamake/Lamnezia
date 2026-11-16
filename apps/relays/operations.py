"""Operation kinds of the relays; registered in `RelaysConfig.ready()`."""

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.groups.models import GroupMember
from apps.operations.services.context import OperationContext
from apps.operations.services.registry import operations
from apps.relays.models import Relay
from apps.relays.services.deployment import (
    INSTALL_KIND,
    REMOVE_KIND,
    UPDATE_KIND,
    RelayDeployment,
)
from apps.relays.services.switch import SWITCH_KIND, RelaySwitch


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


@operations.register(SWITCH_KIND, gettext_lazy("Switch the relay to a member"))
def switch_relay(ctx: OperationContext) -> None:
    """A member that left the group or a relay removed since leave nothing to do."""
    with ctx.step(_("Find the relay and the member")):
        relay = Relay.objects.select_related("server").filter(server_id=ctx.params["server_id"])
        members = GroupMember.objects.select_related("group", "instance__server")
        found_relay = relay.first()
        member = members.filter(pk=ctx.params["member_id"]).first()
        if found_relay is None or member is None:
            ctx.log(_("The relay or the member is gone: nothing to do."))
    if found_relay is not None and member is not None:
        RelaySwitch(found_relay, member, ctx).run()
