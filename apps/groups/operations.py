"""Operation kinds of the server groups; registered in `GroupsConfig.ready()`."""

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.groups.models import GroupMember, MemberState
from apps.groups.services.cloning import GroupJoin, GroupLeave
from apps.groups.services.membership import JOIN_KIND, LEAVE_KIND
from apps.operations.services.context import OperationContext
from apps.operations.services.registry import operations


@operations.register(JOIN_KIND, gettext_lazy("Join a server group"))
def join_group(ctx: OperationContext) -> None:
    member = _member(ctx, MemberState.JOINING)
    if member is not None:
        GroupJoin(member, ctx).run()


@operations.register(LEAVE_KIND, gettext_lazy("Leave a server group"))
def leave_group(ctx: OperationContext) -> None:
    member = _member(ctx, MemberState.LEAVING)
    if member is not None:
        GroupLeave(member, ctx).run()


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
