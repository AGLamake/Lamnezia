from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.audit.services import journal as audit
from apps.audit.tasks import prune_api_call_events
from apps.core.models import Cleanup

pytestmark = pytest.mark.django_db


def journaled(action: str, days_ago: int) -> AuditEvent:
    event = audit.log(action, actor=None)
    AuditEvent.objects.filter(pk=event.pk).update(
        created_at=timezone.now() - timedelta(days=days_ago)
    )
    return event


def test_only_old_api_calls_are_deleted(settings: Any) -> None:
    settings.API_CALL_RETENTION_DAYS = 30
    old_call = journaled("api.call", 31)
    fresh_call = journaled("api.call", 29)
    kept = [journaled(action, 400) for action in ("api.denied", "api.token_create", "keys.show")]

    assert prune_api_call_events() == 1

    remaining = set(AuditEvent.objects.values_list("pk", flat=True))
    assert old_call.pk not in remaining
    assert remaining == {fresh_call.pk, *(event.pk for event in kept)}
    assert Cleanup.objects.get(task="audit.prune_api_calls").removed == 1


def test_the_term_comes_from_the_settings(settings: Any) -> None:
    settings.API_CALL_RETENTION_DAYS = 3
    journaled("api.call", 5)

    assert prune_api_call_events() == 1
    assert not AuditEvent.objects.exists()
