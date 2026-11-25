"""Operation status through the API: only the operations the token's own calls started.

A record holds the kind, status, times and error text; never the parameters, which may carry
secrets, nor the step logs.
"""

from typing import Any

from apps.api.errors import NotFoundError
from apps.api.models import ApiToken
from apps.operations.models import Operation


class OperationRecords:
    def __init__(self, token: ApiToken) -> None:
        self._token = token

    def get(self, pk: int) -> Operation:
        operation = self._token.operations.filter(pk=pk).first()
        if operation is None:
            raise NotFoundError
        return operation

    @staticmethod
    def record(operation: Operation) -> dict[str, Any]:
        return {
            "id": operation.pk,
            "kind": operation.kind,
            "status": operation.status,
            "created_at": operation.created_at,
            "started_at": operation.started_at,
            "finished_at": operation.finished_at,
            "error": (
                {"code": operation.error_code, "message": operation.error_message}
                if operation.error_code or operation.error_message
                else None
            ),
        }
