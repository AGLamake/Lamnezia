"""Scheduled jobs of the audit journal: the retention of API calls."""

from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.audit.services.retention import prune_api_calls
from apps.core.models import Cleanup


@shared_task(name="audit.prune_api_calls")
def prune_api_call_events() -> int:
    before = timezone.now() - timedelta(days=settings.API_CALL_RETENTION_DAYS)
    return Cleanup.record("audit.prune_api_calls", prune_api_calls(before))
