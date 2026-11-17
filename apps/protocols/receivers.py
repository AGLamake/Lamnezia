"""Protocol instances kept up to date by signals of other apps, connected in `ProtocolsConfig`."""

from functools import partial
from typing import Any

from django.db import transaction

from apps.accounts.models import User
from apps.keys.signals import xray_short_id_changed
from apps.protocols.models import ProtocolInstance
from apps.protocols.signals import short_id_changed


def on_xray_short_id_changed(
    sender: object, source: str, short_id: str, owners: list[User], **kwargs: Any
) -> None:
    """Remember the shortId the key profiles now have, so the next change is noticed.

    The owners of the changed keys are told to import them again.
    """
    pk = ProtocolInstance.pk_of(source)
    if pk is None:
        return
    with transaction.atomic():
        instance = ProtocolInstance.objects.select_for_update().filter(pk=pk).first()
        if instance is None:
            return
        if instance.params.get("short_id") != short_id:
            instance.params = {**instance.params, "short_id": short_id}
            instance.save(update_fields=["params"])
    server = instance.server.name
    for owner in owners:
        transaction.on_commit(
            partial(short_id_changed.send, ProtocolInstance, user=owner, server=server)
        )


def connect() -> None:
    xray_short_id_changed.connect(
        on_xray_short_id_changed, dispatch_uid="protocols.xray_short_id_changed"
    )
