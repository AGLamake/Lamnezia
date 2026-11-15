"""Operation kinds of the server groups; registered in `GroupsConfig.ready()`."""

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.accounts.models import User
from apps.groups.models import GroupMember, MemberState, ServerGroup
from apps.groups.services.cloning import GroupInstall, GroupJoin, GroupLeave, MemberCatchUp
from apps.groups.services.handover import HANDOVER_KIND, LeaderHandover
from apps.groups.services.membership import INSTALL_KIND, JOIN_KIND, LEAVE_KIND, SETTINGS_KIND
from apps.groups.services.replication import CATCH_UP_KIND
from apps.groups.services.settings import GroupSettingsChange
from apps.operations.services.context import OperationContext
from apps.operations.services.registry import operations
from apps.protocols.services.awg_settings import AwgSettings
from apps.servers.models import Server


@operations.register(JOIN_KIND, gettext_lazy("Join a server group"))
def join_group(ctx: OperationContext) -> None:
    member = _member(ctx, MemberState.JOINING)
    if member is not None:
        GroupJoin(member, ctx).run()


@operations.register(INSTALL_KIND, gettext_lazy("Install AmneziaWG of a server group"))
def install_member(ctx: OperationContext) -> None:
    with ctx.step(_("Find the group")):
        group = ServerGroup.objects.filter(pk=ctx.params["group_id"]).first()
        if group is None:
            ctx.log(_("The group no longer exists: nothing to do."))
    if group is not None:
        server = Server.objects.get(pk=ctx.params["server_id"])
        GroupInstall(group, server, ctx).run()


@operations.register(LEAVE_KIND, gettext_lazy("Leave a server group"))
def leave_group(ctx: OperationContext) -> None:
    member = _member(ctx, MemberState.LEAVING)
    if member is not None:
        GroupLeave(member, ctx).run()


@operations.register(CATCH_UP_KIND, gettext_lazy("Catch up with the server group"))
def catch_up(ctx: OperationContext) -> None:
    with ctx.step(_("Find the server in the group")):
        member = (
            GroupMember.objects.filter(
                pk=ctx.params["member_id"], state=MemberState.MEMBER, lagging_since__isnull=False
            )
            .select_related("group", "instance__server")
            .first()
        )
        if member is None:
            ctx.log(_("The server no longer lags behind the group: nothing to do."))
    if member is not None:
        MemberCatchUp(member, ctx).run()


@operations.register(HANDOVER_KIND, gettext_lazy("Make a server the leading one of its group"))
def hand_over(ctx: OperationContext) -> None:
    with ctx.step(_("Find the server in the group")):
        member = (
            GroupMember.objects.filter(pk=ctx.params["member_id"], state=MemberState.MEMBER)
            .select_related("group", "instance__server")
            .first()
        )
        if member is None:
            ctx.log(_("The server is no longer in the group: nothing to do."))
        elif member.leading:
            ctx.log(_("The server already leads the group: nothing to do."))
    if member is not None and not member.leading:
        LeaderHandover(member, ctx).run()


@operations.register(SETTINGS_KIND, gettext_lazy("Change the protocol settings of a group"))
def change_group_settings(ctx: OperationContext) -> None:
    """The members that answer take the settings; the others catch up with them later."""
    with ctx.step(_("Find the group")):
        group = ServerGroup.objects.filter(pk=ctx.params["group_id"]).first()
        if group is None:
            ctx.log(_("The group no longer exists: nothing to do."))
    if group is not None:
        actor = User.objects.filter(pk=ctx.params["user_id"]).first()
        GroupSettingsChange(group, ctx).run(
            AwgSettings.from_params(ctx.params), actor=actor, reissue=ctx.params["reissue"]
        )


def _member(ctx: OperationContext, state: MemberState) -> GroupMember | None:
    """The member the operation is for, while it is still in `state`; a retry may find it gone."""
    with ctx.step(_("Find the server in the group")):
        member = (
            GroupMember.objects.filter(pk=ctx.params["member_id"], state=state)
            .select_related("group", "instance__server")
            .first()
        )
        if member is None:
            ctx.log(_("The server is no longer joining or leaving the group: nothing to do."))
    return member
