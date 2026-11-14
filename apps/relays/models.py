"""The relay role of a server and the service peer of a group (docs/FALLBACK.md section 5).

A relay forwards the UDP port of every group that names its server to the live member of the
group; its agent probes the members with the service peer of the group.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.ssh.encryption import EncryptedTextField


class Relay(models.Model):
    """A server that runs the relay container; `installed_at` is empty until it is installed."""

    server = models.OneToOneField(
        "servers.Server",
        verbose_name=_("server"),
        on_delete=models.CASCADE,
        related_name="relay_role",
    )
    installed_at = models.DateTimeField(_("installed at"), null=True, blank=True)
    # Digest of the groups and backends the relay last got; a sync compares it with the panel.
    synced = models.CharField(_("synced digest"), max_length=64, blank=True)
    synced_at = models.DateTimeField(_("synced at"), null=True, blank=True)

    class Meta:
        verbose_name = _("relay")
        verbose_name_plural = _("relays")

    def __str__(self) -> str:
        return str(self.server)

    @property
    def installed(self) -> bool:
        return self.installed_at is not None


class ServicePeer(models.Model):
    """The client of a group the relay probes its members with; it is never given to a user.

    Every member has it as a peer, like the keys of the group.
    """

    group = models.OneToOneField(
        "groups.ServerGroup",
        verbose_name=_("group"),
        on_delete=models.CASCADE,
        related_name="service_peer",
    )
    private_key = EncryptedTextField(_("private key"))
    public_key = models.CharField(_("public key"), max_length=64, unique=True)
    address = models.GenericIPAddressField(_("tunnel address"), protocol="IPv4")

    class Meta:
        verbose_name = _("service peer")
        verbose_name_plural = _("service peers")

    def __str__(self) -> str:
        return f"{self.group} · {self.address}"
