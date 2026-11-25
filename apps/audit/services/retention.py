"""Deleting API calls from the audit journal after their retention term; other events stay."""

from datetime import datetime

from apps.audit.models import AuditEvent

API_CALL = "api.call"


def prune_api_calls(before: datetime) -> int:
    """Delete the API calls journaled before `before`; return how many were deleted.

    Monitoring calls the API every minute, so these records grow by thousands a day. Calls with
    a disabled token and every other event are kept as long as the journal is.
    """
    deleted, _ = AuditEvent.objects.filter(action=API_CALL, created_at__lt=before).delete()
    return deleted
