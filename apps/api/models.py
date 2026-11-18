from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Status


class TokenScope(models.TextChoices):
    """What a token may do. Managing keys and access comes next, through operations only."""

    READ = "read", _("read only")


class ApiToken(models.Model):
    """A token an integration calls the API with (DESIGN.md 5.7).

    Only the SHA-256 of the value is stored, and `prefix` to tell tokens apart in the list. The
    token sees what its `owner` sees: the whole panel or a moderator's zone.
    """

    name = models.CharField(_("name"), max_length=100)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("acts as"),
        on_delete=models.CASCADE,
        related_name="api_tokens",
    )
    scope = models.CharField(
        _("scope"), max_length=16, choices=TokenScope, default=TokenScope.READ
    )
    digest = models.CharField(_("token digest"), max_length=64, unique=True)
    prefix = models.CharField(_("starts with"), max_length=16)
    expires_at = models.DateTimeField(_("expires at"), null=True, blank=True)
    is_active = models.BooleanField(_("active"), default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("created by"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    last_used_at = models.DateTimeField(_("last used at"), null=True, blank=True)

    class Meta:
        verbose_name = _("API token")
        verbose_name_plural = _("API tokens")
        ordering = ("-created_at", "-pk")

    def __str__(self) -> str:
        return f"{self.name} ({self.prefix}…)"

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and timezone.now() >= self.expires_at

    @property
    def works(self) -> bool:
        """Active, in term, and its owner is still a staff member who is not blocked."""
        owner = self.owner
        return (
            self.is_active
            and not self.is_expired
            and owner.is_active
            and owner.is_staff_member
            and owner.status == Status.ACTIVE
        )
