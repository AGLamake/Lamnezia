"""Plans follow server groups: a server that joins a group is given by the group, and the
leading server of a deleted group is given by itself again."""

from typing import Any

from django.db.models.signals import post_save, pre_delete
from django.dispatch import receiver

from apps.groups.models import GroupMember, ServerGroup
from apps.plans.models import Plan


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
