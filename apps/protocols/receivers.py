"""Protocol instances kept up to date by signals of other apps, connected in `ProtocolsConfig`."""

from typing import Any

from django.db import transaction

from apps.keys.signals import xray_short_id_changed
from apps.protocols.models import ProtocolInstance


def on_xray_short_id_changed(sender: object, source: str, short_id: str, **kwargs: Any) -> None:
    """Remember the shortId the key profiles now have, so the next change is noticed."""
    pk = ProtocolInstance.pk_of(source)
    if pk is None:
        return
    with transaction.atomic():
        instance = ProtocolInstance.objects.select_for_update().filter(pk=pk).first()
        if instance is None or instance.params.get("short_id") == short_id:
            return
        instance.params = {**instance.params, "short_id": short_id}
        instance.save(update_fields=["params"])


def connect() -> None:
    xray_short_id_changed.connect(
        on_xray_short_id_changed, dispatch_uid="protocols.xray_short_id_changed"
    )
