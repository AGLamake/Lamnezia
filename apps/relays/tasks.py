from celery import shared_task

from apps.relays.services.deployment import sync_relays


@shared_task(name="relays.sync")
def sync() -> None:
    sync_relays()
