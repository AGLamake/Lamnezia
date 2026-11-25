from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Status
from apps.keys.models import AccessKey
from apps.operations.models import Operation
from apps.servers.models import Server


class Resource(models.TextChoices):
    SERVERS = "servers", _("servers")
    USERS = "users", _("users")
    KEYS = "keys", _("access keys")
    TRAFFIC = "traffic", _("traffic")
    OPERATIONS = "operations", _("operation status")


class Action(models.TextChoices):
    """`manage` includes `read`: a token that changes something also sees it."""

    READ = "read", _("read")
    MANAGE = "manage", _("read and manage")


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
    operations = models.ManyToManyField(Operation, blank=True, related_name="+")

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


class TokenPermission(models.Model):
    """One right of a token: an action on a resource, on one object or on all of the zone.

    At most one of `server`, `user` and `key` is set, and only one a resource allows
    (`apps.api.services.permissions.RULES`). The object going away takes the right with it.
    """

    token = models.ForeignKey(ApiToken, on_delete=models.CASCADE, related_name="permissions")
    resource = models.CharField(_("resource"), max_length=16, choices=Resource)
    action = models.CharField(_("action"), max_length=16, choices=Action, default=Action.READ)
    server = models.ForeignKey(
        Server, verbose_name=_("server"), on_delete=models.CASCADE, null=True, blank=True
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("user"),
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )
    key = models.ForeignKey(
        AccessKey, verbose_name=_("access key"), on_delete=models.CASCADE, null=True, blank=True
    )

    class Meta:
        verbose_name = _("token permission")
        verbose_name_plural = _("token permissions")
        ordering = ("pk",)

    def __str__(self) -> str:
        return f"{self.resource}:{self.action}"
