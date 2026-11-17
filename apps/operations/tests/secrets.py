"""Where an operation could show a secret of its parameters: the check the tests share."""

import json

from django.test import Client

from apps.audit.models import AuditEvent
from apps.operations.models import Operation, OperationStep


def secrets_written(operation: Operation, secret: str, client: Client | None = None) -> list[str]:
    """Where the operation, its steps, the audit or, given a staff `client`, its page carry
    `secret`.
    """
    places = {
        "params": json.dumps(operation.params),
        "error": operation.error_message + operation.error_hint,
        "steps": "".join(
            OperationStep.objects.filter(operation=operation).values_list("log", flat=True)
        ),
        "audit": json.dumps(list(AuditEvent.objects.values_list("data", flat=True))),
    }
    if client is not None:
        places["page"] = client.get(f"/operations/{operation.pk}/").content.decode()
    return [place for place, text in places.items() if secret in text]
