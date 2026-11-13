from celery import shared_task

from apps.keys.services.drift import reconcile_all


@shared_task(name="keys.reconcile")
def reconcile_keys() -> None:
    reconcile_all()
