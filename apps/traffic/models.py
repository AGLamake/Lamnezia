from typing import Any

from django.db import models
from django.utils.translation import gettext_lazy as _

# `source` names the protocol instance a peer lives on; `public_key` is the peer key, which the
# Amnezia app stores as clientId. Together they identify a key without depending on panel models.


class TrafficCounter(models.Model):
    """Last raw counters of a peer, kept to turn the next reading into a delta.

    `active_at` is the latest reading in which counters without a handshake (XRay) grew;
    `present` is whether the latest reading of the source listed the peer at all.
    `key_source` is the source of the key the peer belongs to: the same as `source`, except on a
    clone in a server group, whose peers are keys of the leading server.
    """

    source = models.CharField(_("source"), max_length=100)
    key_source = models.CharField(_("source of the key"), max_length=100)
    public_key = models.CharField(_("public key"), max_length=64)
    rx_bytes = models.PositiveBigIntegerField(_("received, bytes"), default=0)
    tx_bytes = models.PositiveBigIntegerField(_("sent, bytes"), default=0)
    latest_handshake = models.DateTimeField(_("latest handshake"), null=True, blank=True)
    active_at = models.DateTimeField(_("latest activity"), null=True, blank=True)
    present = models.BooleanField(_("on the server"), default=True)
    collected_at = models.DateTimeField(_("collected at"))

    class Meta:
        verbose_name = _("traffic counter")
        verbose_name_plural = _("traffic counters")
        constraints = (
            models.UniqueConstraint(
                fields=["source", "public_key"], name="traffic_counter_unique_peer"
            ),
        )
        indexes = (models.Index(fields=["key_source", "public_key"]),)

    def __str__(self) -> str:
        return f"{self.source} {self.public_key}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        """A counter saved without `key_source` is one of a key of its own source."""
        self.key_source = self.key_source or self.source
        super().save(*args, **kwargs)


class TrafficHourly(models.Model):
    """Traffic of a peer within one hour. rx is what the server received (client upload)."""

    source = models.CharField(_("source"), max_length=100)
    public_key = models.CharField(_("public key"), max_length=64)
    hour = models.DateTimeField(_("hour"))
    rx_bytes = models.PositiveBigIntegerField(_("received, bytes"), default=0)
    tx_bytes = models.PositiveBigIntegerField(_("sent, bytes"), default=0)

    class Meta:
        verbose_name = _("hourly traffic")
        verbose_name_plural = _("hourly traffic")
        constraints = (
            models.UniqueConstraint(
                fields=["source", "public_key", "hour"], name="traffic_hourly_unique_peer_hour"
            ),
        )
        indexes = (models.Index(fields=["hour"]),)

    def __str__(self) -> str:
        return f"{self.source} {self.public_key} {self.hour:%Y-%m-%d %H:00}"


class TrafficSource(models.Model):
    """Whether the last reading of a protocol instance worked.

    An XRay instance answers no reading while its server.json lacks the statistics blocks; its
    traffic is then unknown, not zero. A protocol without counters (OpenVPN, IPsec) has no row.
    `started` is the container's start time from the latest reading: when it changes, the
    container restarted and its counters started from zero.
    """

    source = models.CharField(_("source"), max_length=100, unique=True)
    available = models.BooleanField(_("statistics available"), default=True)
    started = models.CharField(_("container started"), max_length=40, blank=True, default="")
    checked_at = models.DateTimeField(_("checked at"))

    class Meta:
        verbose_name = _("traffic source")
        verbose_name_plural = _("traffic sources")

    def __str__(self) -> str:
        return self.source
