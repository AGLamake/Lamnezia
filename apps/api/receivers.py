"""Model signals of the API, connected in `ApiConfig`: a retry of an operation a token started
belongs to that token too, so the token sees the status of the retry."""

from typing import Any

from django.db.models.signals import post_save

from apps.api.models import ApiToken
from apps.operations.models import Operation


def on_operation_created(sender: object, instance: Operation, created: bool, **kwargs: Any) -> None:
    if not created or instance.retry_of_id is None:
        return
    for token in ApiToken.objects.filter(operations=instance.retry_of_id):
        token.operations.add(instance)


def connect() -> None:
    post_save.connect(on_operation_created, sender=Operation, dispatch_uid="api.operation_created")
