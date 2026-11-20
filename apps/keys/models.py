from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.ssh.encryption import EncryptedTextField


class KeyStatus(models.TextChoices):
    ISSUING = "issuing", _("issuing")
    ACTIVE = "active", _("active")
    DISABLED_LIMIT = "disabled_limit", _("disabled by limit")
    DISABLED_MANUAL = "disabled_manual", _("disabled")
    REVOKING = "revoking", _("revoking")
    REVOKED = "revoked", _("revoked")


DISABLED_STATUSES = frozenset({KeyStatus.DISABLED_LIMIT, KeyStatus.DISABLED_MANUAL})
# Keys a revoke was started for: they take no place in the limits and are never revoked again.
GONE_STATUSES = frozenset({KeyStatus.REVOKING, KeyStatus.REVOKED})


class AccessKey(models.Model):
    """A client (peer) of a protocol instance, DESIGN.md 5.3.

    `source` names the instance, as in the traffic app. Keys imported from a server have
    no private key and no profile: they cannot be shown, only revoked or re-issued. Imported
    XRay keys are the exception: the UUID is all a client needs, so their profile is built later.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("user"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="access_keys",
    )
    source = models.CharField(_("instance"), max_length=100)
    name = models.CharField(_("device name"), max_length=100)
    public_key = models.CharField(_("public key"), max_length=64)
    private_key = EncryptedTextField(_("private key"), blank=True)
    # Taken from the server without the private half: the private key is encrypted, so the
    # database cannot tell an empty one apart.
    imported = models.BooleanField(_("imported"), default=False)
    # Server half of the client profile at issue time (address, port, AWG parameters, keys).
    profile = EncryptedTextField(_("profile"), blank=True)
    # Another address of the server the profile sends clients to; empty: the address of the
    # instance. Taken only from the checked addresses of the server (`Instance.addresses`).
    address = models.CharField(_("server address"), max_length=255, blank=True)
    client_ip = models.GenericIPAddressField(
        _("IP address"), protocol="IPv4", null=True, blank=True
    )
    status = models.CharField(
        _("status"), max_length=16, choices=KeyStatus, default=KeyStatus.ISSUING
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    first_viewed_at = models.DateTimeField(_("first viewed at"), null=True, blank=True)
    # The first reading that saw the key in use after its issue (`key_connected`).
    connected_at = models.DateTimeField(_("first connected at"), null=True, blank=True)
    revoked_at = models.DateTimeField(_("revoked at"), null=True, blank=True)

    class Meta:
        verbose_name = _("access key")
        verbose_name_plural = _("access keys")
        constraints = (
            models.UniqueConstraint(fields=["source", "public_key"], name="keys_unique_peer"),
        )
        indexes = (models.Index(fields=["status"]),)

    def __str__(self) -> str:
        return self.name

    @property
    def can_export(self) -> bool:
        return bool(self.private_key and self.profile) and not self.is_gone

    @property
    def awaits_profile(self) -> bool:
        """An imported XRay key: its UUID is known, the profile is built when it is first shown."""
        in_force = self.status == KeyStatus.ACTIVE or self.is_disabled
        return bool(self.private_key) and not self.profile and in_force

    @property
    def is_disabled(self) -> bool:
        return self.status in DISABLED_STATUSES

    @property
    def is_gone(self) -> bool:
        """Revoked, or its revoke is under way."""
        return self.status in GONE_STATUSES


class DriftKind(models.TextChoices):
    UNKNOWN_PEER = "unknown_peer", _("added outside the panel")
    MISSING = "missing", _("removed outside the panel")
    UNEXPECTED = "unexpected", _("off in the panel, but on the server")


class Drift(models.Model):
    """A difference between the panel and a server, found by the latest reconciliation.

    The rows of an instance are replaced on every reconciliation, so they always describe the
    latest reading. `key` is empty for a peer the panel does not know.
    """

    source = models.CharField(_("instance"), max_length=100)
    public_key = models.CharField(_("public key"), max_length=64)
    kind = models.CharField(_("difference"), max_length=16, choices=DriftKind)
    key = models.ForeignKey(
        AccessKey,
        verbose_name=_("key"),
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="drifts",
    )
    name = models.CharField(_("name"), max_length=100, blank=True)
    client_ip = models.GenericIPAddressField(
        _("IP address"), protocol="IPv4", null=True, blank=True
    )
    detected_at = models.DateTimeField(_("found at"))

    class Meta:
        verbose_name = _("difference with the server")
        verbose_name_plural = _("differences with the server")
        ordering = ("source", "kind", "name")
        constraints = (
            models.UniqueConstraint(fields=["source", "public_key"], name="keys_drift_unique_peer"),
        )

    def __str__(self) -> str:
        return f"{self.name or self.public_key}: {self.get_kind_display()}"


class Subscription(models.Model):
    """A user's subscription link: XRay clients fetch the user's keys by it and keep them fresh.

    The token is the secret of the link; it is stored encrypted, and `digest` finds it.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name=_("user"),
        on_delete=models.CASCADE,
        related_name="subscription",
    )
    token = EncryptedTextField(_("token"))
    digest = models.CharField(_("token digest"), max_length=64, unique=True)
    created_at = models.DateTimeField(_("created at"), auto_now=True)

    class Meta:
        verbose_name = _("subscription link")
        verbose_name_plural = _("subscription links")

    def __str__(self) -> str:
        return str(self.user)


class SubscriptionDevice(models.Model):
    """A device that fetched a subscription link with its `x-hwid`; the plan limits their number.

    Only a digest of the device ID is kept: the panel matches it, never shows it. Model, system
    and app are what the client said about itself. Request addresses are not stored.
    """

    subscription = models.ForeignKey(
        Subscription,
        verbose_name=_("subscription link"),
        on_delete=models.CASCADE,
        related_name="devices",
    )
    digest = models.CharField(_("device ID digest"), max_length=64)
    model = models.CharField(_("device model"), max_length=100, blank=True)
    system = models.CharField(_("system"), max_length=100, blank=True)
    app = models.CharField(_("app"), max_length=100, blank=True)
    first_seen = models.DateTimeField(_("first seen"), auto_now_add=True)
    last_seen = models.DateTimeField(_("last seen"))

    class Meta:
        verbose_name = _("subscription device")
        verbose_name_plural = _("subscription devices")
        ordering = ("first_seen", "pk")
        constraints = (
            models.UniqueConstraint(
                fields=("subscription", "digest"), name="keys_subscription_device_unique"
            ),
        )

    def __str__(self) -> str:
        return self.model or self.app or str(_("Unknown device"))
