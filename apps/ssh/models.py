from django.db import models
from django.utils.translation import gettext_lazy as _


class KnownHost(models.Model):
    """Server host key fingerprint saved on the first connection (trust on first use)."""

    host = models.CharField(_("host"), max_length=255)
    port = models.PositiveIntegerField(_("port"))
    key_type = models.CharField(_("key type"), max_length=64)
    fingerprint = models.CharField(_("fingerprint"), max_length=128)
    created_at = models.DateTimeField(_("first seen"), auto_now_add=True)

    class Meta:
        verbose_name = _("known host")
        verbose_name_plural = _("known hosts")
        constraints = (
            models.UniqueConstraint(fields=["host", "port"], name="ssh_knownhost_unique_address"),
        )

    def __str__(self) -> str:
        return f"{self.host}:{self.port} {self.fingerprint}"
