from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from amnezia_core.awg_export import AWG_CONTAINER
from amnezia_core.containers import AWG_LEGACY, WIREGUARD
from amnezia_core.ipsec import IPSEC_CONTAINER
from amnezia_core.openvpn import OPENVPN_CONTAINER
from amnezia_core.xray import XRAY_CONTAINER


class Protocol(models.TextChoices):
    """Protocols a plan can allow, by the name of their container."""

    AWG2 = AWG_CONTAINER, "AmneziaWG 2.0"
    AWG_LEGACY = AWG_LEGACY.name, _("AmneziaWG (legacy)")
    WIREGUARD = WIREGUARD.name, "WireGuard"
    XRAY = XRAY_CONTAINER, "XRay (VLESS Reality)"
    OPENVPN = OPENVPN_CONTAINER, "OpenVPN"
    IPSEC = IPSEC_CONTAINER, "IPsec"


class Plan(models.Model):
    """A tariff and a group at once: servers, protocols and limits (DESIGN.md 5.3, 6.4).

    An empty limit means no limit; no servers or protocols selected means all of them.
    Traffic limits are in bytes; the speed limit is per key, in Mbit/s each way.
    `price` and `currency` are informational only.
    """

    name = models.CharField(_("name"), max_length=100, unique=True)
    traffic_limit = models.PositiveBigIntegerField(_("traffic per period"), null=True, blank=True)
    period_days = models.PositiveSmallIntegerField(
        _("period, days"), default=30, validators=[MinValueValidator(1)]
    )
    total_traffic_limit = models.PositiveBigIntegerField(
        _("traffic in total"), null=True, blank=True
    )
    max_keys = models.PositiveSmallIntegerField(_("keys at most"), null=True, blank=True)
    speed_limit = models.PositiveIntegerField(
        _("speed, Mbit/s"), null=True, blank=True, validators=[MinValueValidator(1)]
    )
    access_days = models.PositiveSmallIntegerField(
        _("access, days"),
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
        help_text=_("Counted from the day the plan is assigned. Empty: no end date."),
    )
    servers = models.ManyToManyField(
        "servers.Server",
        verbose_name=_("servers"),
        blank=True,
        related_name="plans",
        help_text=_("None selected: all servers."),
    )
    protocols = models.JSONField(
        _("protocols"), default=list, blank=True, help_text=_("None selected: all protocols.")
    )
    is_trial = models.BooleanField(_("trial"), default=False)
    price = models.DecimalField(_("price"), max_digits=10, decimal_places=2, null=True, blank=True)
    currency = models.CharField(_("currency"), max_length=3, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("plan")
        verbose_name_plural = _("plans")
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name


class UserLimits(models.Model):
    """A user's plan and personal overrides of its limits.

    An empty override takes the plan's value. `access_until` replaces the end date the
    plan's `access_days` give.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name=_("user"),
        on_delete=models.CASCADE,
        related_name="limits",
    )
    plan = models.ForeignKey(
        Plan,
        verbose_name=_("plan"),
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="subscriptions",
    )
    # Start of the access term and of the first traffic period.
    plan_assigned_at = models.DateTimeField(_("plan assigned at"), null=True, blank=True)
    traffic_limit = models.PositiveBigIntegerField(_("traffic per period"), null=True, blank=True)
    period_days = models.PositiveSmallIntegerField(
        _("period, days"), null=True, blank=True, validators=[MinValueValidator(1)]
    )
    total_traffic_limit = models.PositiveBigIntegerField(
        _("traffic in total"), null=True, blank=True
    )
    max_keys = models.PositiveSmallIntegerField(_("keys at most"), null=True, blank=True)
    speed_limit = models.PositiveIntegerField(
        _("speed, Mbit/s"), null=True, blank=True, validators=[MinValueValidator(1)]
    )
    access_until = models.DateTimeField(_("access until"), null=True, blank=True)

    class Meta:
        verbose_name = _("user limits")
        verbose_name_plural = _("user limits")

    def __str__(self) -> str:
        return str(self.user)
