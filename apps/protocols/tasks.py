"""Beat tasks of the protocols: removing the old outcomes of speed tests."""

from celery import shared_task
from django.utils import timezone

from apps.core.models import Cleanup
from apps.protocols.services.speed_results import KEEP, prune


@shared_task(name="protocols.prune_speed_results")
def prune_speed_results() -> int:
    return Cleanup.record("protocols.prune_speed_results", prune(timezone.now() - KEEP))
