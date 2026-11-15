from celery import shared_task

from apps.groups.services.replication import catch_up_lagging, check_replicas


@shared_task(name="groups.catch_up")
def catch_up_groups() -> None:
    catch_up_lagging()


@shared_task(name="groups.check_replicas")
def check_group_replicas() -> None:
    check_replicas()
