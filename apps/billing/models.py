import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class OrderStatus(models.TextChoices):
    PENDING = "pending", _("waiting for payment")
    PAID = "paid", _("paid")
    FAILED = "failed", _("not paid")
    EXPIRED = "expired", _("expired")
    REFUNDED = "refunded", _("refunded")


class Order(models.Model):
    """A purchase of a plan through a payment provider, at the price and term of the day.

    `number` is the public id: it goes to the provider and into page addresses, so orders
    cannot be counted or guessed. `external_id` is the provider's own id of the payment.
    `days` empty means access without an end date. `username` keeps who paid after the user is
    deleted, so the history of payments stays whole.
    """

    number = models.UUIDField(_("number"), default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("user"),
        on_delete=models.SET_NULL,
        null=True,
        related_name="orders",
    )
    username = models.CharField(_("username"), max_length=150, blank=True)
    plan = models.ForeignKey(
        "plans.Plan", verbose_name=_("plan"), on_delete=models.PROTECT, related_name="orders"
    )
    provider = models.CharField(_("payment method"), max_length=50)
    amount = models.DecimalField(_("amount"), max_digits=10, decimal_places=2)
    currency = models.CharField(_("currency"), max_length=3)
    days = models.PositiveSmallIntegerField(_("access, days"), null=True, blank=True)
    status = models.CharField(
        _("status"), max_length=16, choices=OrderStatus, default=OrderStatus.PENDING
    )
    external_id = models.CharField(_("payment id"), max_length=200, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    paid_at = models.DateTimeField(_("paid at"), null=True, blank=True)

    class Meta:
        verbose_name = _("order")
        verbose_name_plural = _("orders")
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.plan} · {self.amount} {self.currency}"

    @property
    def buyer(self) -> str:
        """Who paid, also after their account is deleted."""
        return self.user.username if self.user else self.username
