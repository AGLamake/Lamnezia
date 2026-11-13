from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class AuditEvent(models.Model):
    """Who did what to which object, from where. Rows are only added, never changed.

    The actor name and the target description are copied, so the record stays readable
    after the user or the object is deleted.
    """

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("actor"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    actor_name = models.CharField(_("actor name"), max_length=150, blank=True)
    action = models.CharField(_("action"), max_length=64)
    target_type = models.CharField(_("target type"), max_length=64, blank=True)
    target_id = models.CharField(_("target id"), max_length=64, blank=True)
    target_repr = models.CharField(_("target"), max_length=200, blank=True)
    data = models.JSONField(_("details"), default=dict, blank=True)
    ip = models.GenericIPAddressField(_("IP address"), null=True, blank=True)
    created_at = models.DateTimeField(_("time"), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("audit event")
        verbose_name_plural = _("audit events")
        ordering = ("-created_at", "-pk")
        indexes = (models.Index(fields=["action", "created_at"]),)

    def __str__(self) -> str:
        return f"{self.actor_name or '—'} {self.action} {self.target_repr}"
