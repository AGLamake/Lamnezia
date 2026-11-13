"""How many keys an instance serves, for warnings before a change of the instance."""

from dataclasses import dataclass

from django.db.models import Count, Q

from apps.keys.models import AccessKey, KeyStatus


@dataclass(frozen=True)
class InstanceKeyCount:
    """Keys of an instance that are not revoked; `imported` came from the server without a
    private key, so the panel cannot reissue their profiles."""

    total: int
    imported: int

    @classmethod
    def of(cls, source: str) -> "InstanceKeyCount":
        counts = (
            AccessKey.objects.filter(source=source)
            .exclude(status=KeyStatus.REVOKED)
            .aggregate(total=Count("pk"), imported=Count("pk", filter=Q(imported=True)))
        )
        return cls(**counts)
