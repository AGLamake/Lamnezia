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
    they are what brings the keys back on a new VPS. Its protocols are removed first. A row
    with no copy in it, only a note that the copy was too large, keeps nothing.
    """
    origin = collector.origin
    deletes_server = (
        issubclass(origin.model, Server)
        if isinstance(origin, models.QuerySet)
        else isinstance(origin, Server)
    )
    kept = {backup for backup in sub_objs if getattr(backup, "has_copy", True)}
    if deletes_server and kept:
        raise models.ProtectedError(
            "the server has backups; remove its protocols and services first", kept
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


class SnapshotReason(models.TextChoices):
    GROUP_JOIN = "group_join", _("before joining a server group")
    GROUP_LEAVE = "group_leave", _("before leaving a server group")
    SNAPSHOT_RESTORE = "restore", _("before restoring a snapshot")
    JOIN_CANCEL = "join_cancel", _("before taking a joining server out of its group")


class ProtocolSnapshot(models.Model):
    """The AmneziaWG 2.0 files of a server taken before a change replaces its identity.

    Unlike `ProtocolBackup`, which the hourly copy refreshes, a snapshot is never overwritten:
    it keeps the server key, the preshared key and the clients the server had before it joined
    or left a group, before a failed join was rolled back, or before another snapshot was
    restored on it. Stored encrypted; deleting
    the server refuses while a snapshot is there.
    """

    instance = models.ForeignKey(
        "protocols.ProtocolInstance",
        verbose_name=_("protocol"),
        on_delete=keep_from_server_deletion,
        related_name="snapshots",
    )
    reason = models.CharField(_("reason"), max_length=16, choices=SnapshotReason)
    config = EncryptedTextField(_("server configuration"))
    clients_table = EncryptedTextField(_("client list"), blank=True)
    preshared_key = EncryptedTextField(_("preshared key"), blank=True)
    taken_at = models.DateTimeField(_("saved at"), auto_now_add=True)

    class Meta:
        verbose_name = _("protocol snapshot")
        verbose_name_plural = _("protocol snapshots")
        ordering = ("-taken_at", "-pk")

    def __str__(self) -> str:
        return f"{self.instance} @ {self.taken_at:%Y-%m-%d %H:%M}"

    @property
    def holds_group_files(self) -> bool:
        """Taken while the server ran the files of a group: restoring it brings the group key."""
        return self.reason in {SnapshotReason.GROUP_LEAVE, SnapshotReason.JOIN_CANCEL}


class ServiceBackup(models.Model):
    """The state folders of a service container as read last time, and what the reads found.

    The onion keys and the WordPress site of Tor, the secret and settings of a Telegram proxy,
    the host keys and uploads of SFTP: a restore puts them into the new container, so the
    address, the links and the account stay the same. The tar.gz is a file on the panel disk,
    encrypted under a key of its own kept in `file_key` (`services.service_copies`); a copy
    saved by an older panel stays base64 in `archive` until the next one replaces it.

    `digest` is the fingerprint the files had when copied: a read that finds the same one only
    sets `checked_at`. A copy over the size limit is not taken; `too_large_size` says how
    large the files were until a copy fits again. The panel forgets the row with the service,
    and deleting the server refuses while there is a copy.
    """

    service = models.OneToOneField(
        "protocols.ServiceInstance",
        verbose_name=_("service"),
        on_delete=keep_from_server_deletion,
        related_name="backup",
    )
    archive = EncryptedTextField(_("container files"), blank=True)
    file = models.FileField(_("copy file"), upload_to="service-backups/", blank=True)
    file_key = EncryptedTextField(_("copy file key"), blank=True)
    digest = models.CharField(_("fingerprint of the files"), max_length=64, blank=True)
    taken_at = models.DateTimeField(_("saved at"), null=True, blank=True)
    checked_at = models.DateTimeField(_("checked at"), null=True, blank=True)
    too_large_at = models.DateTimeField(_("found too large at"), null=True, blank=True)
    too_large_size = models.PositiveBigIntegerField(_("size not copied"), null=True, blank=True)

    class Meta:
        verbose_name = _("service backup")
        verbose_name_plural = _("service backups")

    def __str__(self) -> str:
        taken_at = f"{self.taken_at:%Y-%m-%d %H:%M}" if self.taken_at else "—"
        return f"{self.service} @ {taken_at}"

    @property
    def has_copy(self) -> bool:
        return bool(self.file or self.archive)

    @property
    def unchanged_since(self) -> bool:
        """The files were read again after the copy and had not changed."""
        return bool(self.taken_at and self.checked_at and self.checked_at > self.taken_at)
