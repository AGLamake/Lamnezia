"""Plans follow server groups and their staff: a server that joins a group is given by the
group, the leading server of a deleted group is given by itself again, and a change of a plan's
staff relinks its users."""

from typing import Any

from django.db.models.signals import m2m_changed, post_save, pre_delete
from django.dispatch import receiver

from apps.accounts.models import User
from apps.groups.models import GroupMember, ServerGroup
from apps.plans.models import Plan
from apps.plans.services.binding import PlanBinding


@receiver(post_save, sender=GroupMember)
def server_joined(
    sender: type[GroupMember], instance: GroupMember, created: bool, **_: Any
) -> None:
    if not created:
        return
    server_id = instance.instance.server_id
    for plan in Plan.objects.filter(servers=server_id):
        plan.groups.add(instance.group_id)
        plan.servers.remove(server_id)


@receiver(pre_delete, sender=ServerGroup)
def group_deleted(sender: type[ServerGroup], instance: ServerGroup, **_: Any) -> None:
    leader = instance.leader
    if leader is None:
        return
    for plan in instance.plans.all():
        plan.servers.add(leader.instance.server_id)


@receiver(m2m_changed, sender=Plan.staff.through)
def staff_changed(
    sender: type,
    instance: Plan | User,
    action: str,
    reverse: bool,
    pk_set: set[int] | None,
    **_: Any,
) -> None:
    """Either side may change: `plan.staff` or `member.staffed_plans`."""
    if not reverse:
        if action in {"post_add", "post_remove", "post_clear"}:
            PlanBinding.resync(instance)  # type: ignore[arg-type]
        return
    if action == "pre_clear":
        # The plans are gone once cleared, so they are noted beforehand.
        instance._cleared_plans = list(instance.staffed_plans.all())  # type: ignore[union-attr]
    elif action == "post_clear":
        for plan in getattr(instance, "_cleared_plans", []):
            PlanBinding.resync(plan)
    elif action in {"post_add", "post_remove"}:
        for plan in Plan.objects.filter(pk__in=pk_set or ()):
            PlanBinding.resync(plan)
