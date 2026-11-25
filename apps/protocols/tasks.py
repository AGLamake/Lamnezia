"""Beat tasks of the protocols: old outcomes of speed tests, the metadata block of old servers."""

from celery import shared_task
from django.utils import timezone

from apps.core.models import Cleanup
from apps.protocols.services import metadata_block
from apps.protocols.services.speed_results import KEEP, prune


@shared_task(name="protocols.prune_speed_results")
def prune_speed_results() -> int:
    return Cleanup.record("protocols.prune_speed_results", prune(timezone.now() - KEEP))


@shared_task(name="protocols.metadata_catch_up")
def metadata_catch_up() -> int:
    """Close the servers without the current metadata block script; the operations started."""
    return len(metadata_block.catch_up())
