"""Copies of the protocol and service files kept in the panel for a restore."""

from typing import Any

from django.db import models
from django.db.models.deletion import Collector
from django.utils.translation import gettext_lazy as _

from apps.servers.models import Server
from apps.ssh.encryption import EncryptedTextField


def keep_from_server_deletion(
    collector: Collector,
    field: "models.Field[Any, Any]",
    sub_objs: "models.QuerySet[Any]",
    using: str,
) -> None:
    """A copy goes with the protocol or service the panel forgets; deleting a server refuses.

    A server deleted in one go would take the copies of all its protocols with it unnoticed:
    they are what brings the keys back on a new VPS. Its protocols are removed first.
    """
    origin = collector.origin
    deletes_server = (
        issubclass(origin.model, Server)
        if isinstance(origin, models.QuerySet)
        else isinstance(origin, Server)
    )
    if deletes_server:
        raise models.ProtectedError(
            "the server has backups; remove its protocols and services first", set(sub_objs)
        )
    models.CASCADE(collector, field, sub_objs, using)


class ProtocolBackup(models.Model):
    """The server files of a protocol as read last time; a restore puts them on a new server.

    AmneziaWG 2.0 keeps the config and clientsTable; the other protocols keep `archive`, a tar.gz
    of the container state folders (amnezia_core.container_archive). The files hold the server
    private keys, so all of them are stored encrypted. Deleting the server refuses while
    the copy is there (`keep_from_server_deletion`).
    """

    instance = models.OneToOneField(
        "protocols.ProtocolInstance",
        verbose_name=_("protocol"),
        on_delete=keep_from_server_deletion,
        related_name="backup",
    )
    config = EncryptedTextField(_("server configuration"), blank=True)
    clients_table = EncryptedTextField(_("client list"), blank=True)
    archive = EncryptedTextField(_("container files"), blank=True)
    taken_at = models.DateTimeField(_("saved at"))

    class Meta:
        verbose_name = _("protocol backup")
        verbose_name_plural = _("protocol backups")

    def __str__(self) -> str:
        return f"{self.instance} @ {self.taken_at:%Y-%m-%d %H:%M}"


class ServiceBackup(models.Model):
    """The state folders of a service container as read last time: a tar.gz in `archive`.

    The onion keys of the Tor site, the secret and settings of a Telegram proxy, the host keys
    of SFTP: a restore puts them into the new container, so the address, the links and the
    account stay the same. Stored encrypted; the panel forgets it with the service, and
    deleting the server refuses while the copy is there.
    """

    service = models.OneToOneField(
        "protocols.ServiceInstance",
        verbose_name=_("service"),
        on_delete=keep_from_server_deletion,
        related_name="backup",
    )
    archive = EncryptedTextField(_("container files"))
    taken_at = models.DateTimeField(_("saved at"))

    class Meta:
        verbose_name = _("service backup")
        verbose_name_plural = _("service backups")

    def __str__(self) -> str:
        return f"{self.service} @ {self.taken_at:%Y-%m-%d %H:%M}"
