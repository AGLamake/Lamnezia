from celery import shared_task

from apps.backups.services.freshness import take_fresh_copy, take_fresh_service_copy
from apps.backups.services.snapshot import save_all_backups


@shared_task(name="backups.save_protocols")
def save_protocol_backups() -> None:
    save_all_backups()


@shared_task(name="backups.save_protocol")
def save_protocol_backup(instance_id: int) -> None:
    """The fresh copy of one protocol after its keys changed."""
    take_fresh_copy(instance_id)


@shared_task(name="backups.save_service")
def save_service_backup(service_id: int) -> None:
    """The fresh copy of one service after its secret or settings changed."""
    take_fresh_service_copy(service_id)
