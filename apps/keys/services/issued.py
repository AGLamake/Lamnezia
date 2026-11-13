"""Saving keys the server has just issued, with the server address their instance has now.

An IPsec key names the IP of the server certificate. The IP is read from the server while the
key is issued, and a reissue of the certificate may finish before the key is saved: it stores
the new IP on the instance and rewrites the profiles of the keys it finds. Both sides lock the
key rows, so a key saved first is found by the reissue, and a key saved after it reads the new
IP here.
"""

from collections.abc import Sequence
from dataclasses import replace

from django.db import transaction

from amnezia_core.ipsec import IpsecContainer
from apps.keys.models import AccessKey
from apps.keys.services.instances import instances
from apps.keys.services.profile import IpsecKeyProfile

ISSUED_FIELDS = ("client_ip", "private_key", "profile", "status")


class IssuedKeys:
    """Keys with the fields the backend filled, saved in one transaction."""

    def __init__(self, keys: Sequence[AccessKey]) -> None:
        self._keys = keys

    def save(self) -> None:
        with transaction.atomic():
            pks = [key.pk for key in self._keys]
            list(AccessKey.objects.select_for_update().filter(pk__in=pks).values_list("pk"))
            for key in self._keys:
                self._refresh(key)
                key.save(update_fields=list(ISSUED_FIELDS))

    @staticmethod
    def _refresh(key: AccessKey) -> None:
        """An IPsec profile gets the address and the certificate IP the instance has now."""
        if not key.profile:
            return
        instance = instances.get(key.source)
        if not isinstance(instance.container, IpsecContainer) or not instance.server_ip:
            return
        profile = IpsecKeyProfile.from_json(key.profile)
        current = replace(profile, host=instance.host, server_ip=instance.server_ip)
        key.profile = current.to_json()
