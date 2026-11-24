import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from apps.notifications.events import EVENTS, Event
from apps.ssh.encryption import EncryptedTextField


class ChannelKind(models.TextChoices):
    EMAIL = "email", _("Email")
    TELEGRAM = "telegram", "Telegram"
    DISCORD = "discord", "Discord"
    DISCORD_DM = "discord_dm", _("Discord direct message")
    WEBHOOK = "webhook", _("Webhook")
    BROWSER = "browser", _("Browser push")


class Channel(models.Model):
    """Where administrators get notifications. What `target` and `secret` hold depends on the kind:

    email — addresses / —; Telegram — chat_id / bot token (empty: the panel bot);
    Discord — — / webhook URL; Discord direct message — user ID / — (the panel bot);
    webhook — URL / signing secret; browser push — — / — (the browsers of the staff, each within
    their zone).
    """

    name = models.CharField(_("name"), max_length=100)
    kind = models.CharField(_("kind"), max_length=16, choices=ChannelKind.choices)
    enabled = models.BooleanField(_("enabled"), default=True)
    target = models.CharField(_("address"), max_length=500, blank=True)
    secret = EncryptedTextField(_("secret"), blank=True)
    created_at = models.DateTimeField(_("added at"), auto_now_add=True)

    class Meta:
        verbose_name = _("notification channel")
        verbose_name_plural = _("notification channels")
        ordering = ("kind", "name")

    def __str__(self) -> str:
        return f"{self.get_kind_display()}: {self.name}"


class Route(models.Model):
    """A rule: notifications of `event` go to `channel`."""

    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="routes")
    event = models.CharField(_("event"), max_length=64)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=["channel", "event"], name="notifications_route_unique"),
        )

    def __str__(self) -> str:
        return f"{self.event} → {self.channel}"


class Notification(models.Model):
    """One event that happened. `key` makes a repeated emit of the same event a no-op."""

    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    event = models.CharField(_("event"), max_length=64)
    key = models.CharField(max_length=200, unique=True, null=True, blank=True)
    data = models.JSONField(_("details"), default=dict)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("user"),
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )
    # The messenger the user hears of it in instead of the one they chose (an answer of support
    # goes where they wrote from); empty: their choice.
    via = models.CharField(max_length=16, blank=True)
    # The user's channel (`NotifyVia`) the message reached them by; empty until one took it.
    sent_via = models.CharField(max_length=16, blank=True)
    # The user's channel left out: it already has what the message tells of.
    skip_via = models.CharField(max_length=16, blank=True)
    created_at = models.DateTimeField(_("time"), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("notification")
        verbose_name_plural = _("notifications")

    def __str__(self) -> str:
        return f"{self.event} {self.uid}"

    @property
    def event_label(self) -> str:
        return str(EVENTS[Event(self.event)].label) if self.event in Event else self.event


class Trouble(models.Model):
    """An outage or lag going on now, by the key of the event that told it; the row is kept
    whether anybody heard of the event or not and goes when the recovery is told."""

    key = models.CharField(max_length=200, unique=True)
    started_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.key


class DeliveryStatus(models.TextChoices):
    PENDING = "pending", _("Waiting")
    SENDING = "sending", _("Sending")
    SENT = "sent", _("Sent")
    FAILED = "failed", _("Failed")


class Delivery(models.Model):
    """A notification on its way to one channel, or to the user it is about (`to_user`)."""

    notification = models.ForeignKey(
        Notification, on_delete=models.CASCADE, related_name="deliveries"
    )
    channel = models.ForeignKey(
        Channel,
        verbose_name=_("channel"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deliveries",
    )
    to_user = models.BooleanField(_("to the user"), default=False)
    # To the browsers the user it is about subscribed (`PushSubscription`), besides `to_user`.
    to_browser = models.BooleanField(_("to the user's browsers"), default=False)
    # Copied, so the log stays readable after the channel is deleted.
    channel_name = models.CharField(_("channel"), max_length=150)
    status = models.CharField(
        _("status"), max_length=16, choices=DeliveryStatus.choices, default=DeliveryStatus.PENDING
    )
    attempts = models.PositiveSmallIntegerField(_("attempts"), default=0)
    error = models.CharField(_("error"), max_length=300, blank=True)
    created_at = models.DateTimeField(_("time"), auto_now_add=True, db_index=True)
    sent_at = models.DateTimeField(_("sent at"), null=True, blank=True)
    # When a worker took the delivery to send: one still sending long after it lost the worker.
    claimed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("delivery")
        verbose_name_plural = _("deliveries")
        ordering = ("-created_at", "-pk")
        constraints = (
            models.UniqueConstraint(
                fields=["notification", "channel"],
                condition=Q(channel__isnull=False),
                name="notifications_delivery_once_per_channel",
            ),
            models.UniqueConstraint(
                fields=["notification"],
                condition=Q(to_user=True),
                name="notifications_delivery_once_to_user",
            ),
            models.UniqueConstraint(
                fields=["notification"],
                condition=Q(to_browser=True),
                name="notifications_delivery_once_to_browser",
            ),
        )

    def __str__(self) -> str:
        return f"{self.notification} → {self.channel_name}"


class VapidKey(models.Model):
    """The panel's one key pair for Web Push, made when it is first needed. A new pair would
    leave every browser subscribed with the old public key without notifications.
    """

    public_key = models.CharField(max_length=100)
    private_key = EncryptedTextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.public_key


class PushSubscription(models.Model):
    """A browser or an installed app that takes Web Push notifications for `user`.

    `endpoint` is the address at the push service of the browser; a second subscription from the
    same browser replaces the first, even for another account.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="push_subscriptions"
    )
    endpoint = models.URLField(max_length=1000, unique=True)
    p256dh = models.CharField(max_length=100)
    auth = EncryptedTextField()
    device = models.CharField(_("browser"), max_length=100, blank=True)
    created_at = models.DateTimeField(_("subscribed at"), auto_now_add=True)
    # When the push service last took a push for it: whether the browser showed it, it does not say.
    delivered_at = models.DateTimeField(_("last delivered"), null=True, blank=True)

    class Meta:
        verbose_name = _("browser subscription")
        verbose_name_plural = _("browser subscriptions")
        ordering = ("-created_at", "-pk")

    def __str__(self) -> str:
        return f"{self.user} {self.device}".strip()
