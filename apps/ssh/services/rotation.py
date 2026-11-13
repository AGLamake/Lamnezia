"""Re-encrypting every stored secret with the current first LAMNEZIA_ENCRYPTION_KEY."""

from django.db import models, transaction
from django.db.models import Q

from apps.operations.models import Operation
from apps.ssh.encryption import SecretCipher, encrypted_models


class SecretRotation:
    """Rewrites encrypted fields: reading decrypts with any key, writing uses the first one.

    Secrets in operation parameters (Operation.SECRET_PARAMS) are rewritten too: a retry copies
    the parameters of the old operation and must still decrypt them.
    """

    def run(self) -> int:
        """Re-encrypt all rows and return how many were rewritten."""
        rows = sum(self._rotate(model, fields) for model, fields in encrypted_models())
        return rows + self._rotate_params()

    @staticmethod
    def _rotate(model: type[models.Model], fields: list[str]) -> int:
        manager = model._default_manager
        with transaction.atomic():
            # Locked rows: a background operation cannot change a secret between read and write.
            rows = manager.select_for_update().only("pk", *fields)
            for row in rows:
                manager.filter(pk=row.pk).update(**{name: getattr(row, name) for name in fields})
            return len(rows)

    @staticmethod
    def _rotate_params() -> int:
        cipher = SecretCipher.from_settings()
        with_secrets = Q()
        for key in Operation.SECRET_PARAMS:
            with_secrets |= Q(params__has_key=key)
        with transaction.atomic():
            operations = Operation.objects.select_for_update().filter(with_secrets).only("params")
            for operation in operations:
                params = dict(operation.params)
                for key in Operation.SECRET_PARAMS:
                    if params.get(key):
                        params[key] = cipher.rotate(params[key])
                Operation.objects.filter(pk=operation.pk).update(params=params)
            return len(operations)
