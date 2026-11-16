from celery import shared_task

from apps.groups.models import ServerGroup
from apps.groups.services.addressing import DomainCheck
from apps.groups.services.replication import catch_up_lagging, check_replicas


@shared_task(name="groups.catch_up")
def catch_up_groups() -> None:
    catch_up_lagging()


@shared_task(name="groups.check_replicas")
def check_group_replicas() -> None:
    check_replicas()


@shared_task(name="groups.check_domain")
def check_group_domain(group_id: int) -> None:
    group = ServerGroup.objects.select_related("relay").filter(pk=group_id).first()
    if group is not None:
        DomainCheck(group).run()
