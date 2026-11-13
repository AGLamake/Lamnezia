from datetime import datetime
from typing import Any

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Messenger


class BotStatus(models.TextChoices):
    NO_TOKEN = "no_token", _("no token")
    OFF = "off", _("turned off")
    STARTING = "starting", _("starting")
    RUNNING = "running", _("running")
    ERROR = "error", _("error")


class BotRuntime(models.Model):
    """One messenger bot: what the administrator wants of it and what the bots service reported.

    The panel writes `enabled`, `restart_requested_at` and `leave_requests`; the bots service
    reads them and writes the rest every BOT_SUPERVISOR_SECONDS (`seen_at` is its heartbeat).
    """

    messenger = models.CharField(
        _("messenger"), max_length=16, choices=Messenger.choices, unique=True
    )
    enabled = models.BooleanField(_("enabled"), default=True)
    restart_requested_at = models.DateTimeField(null=True, blank=True)
    # Ids of servers (Discord guilds) the administrator asked the bot to leave.
    leave_requests = models.JSONField(default=list, blank=True)

    status = models.CharField(
        _("status"), max_length=16, choices=BotStatus.choices, default=BotStatus.OFF
    )
    identity = models.CharField(_("bot"), max_length=200, blank=True)
    error = models.TextField(_("error"), blank=True)
    # [{"id": ..., "name": ..., "members": ...}] of the servers the bot is on.
    servers = models.JSONField(default=list, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("bot")
        verbose_name_plural = _("bots")

    def __str__(self) -> str:
        return f"{Messenger(self.messenger).label} bot"

    @classmethod
    def of(cls, messenger: str) -> "BotRuntime":
        runtime, _created = cls.objects.get_or_create(messenger=messenger)
        return runtime

    def is_responding(self, now: datetime) -> bool:
        """The bots service reported lately: it runs and the status is current."""
        return self.seen_at is not None and now - self.seen_at <= settings.BOT_SUPERVISOR_STALE

    def server(self, server_id: str) -> dict[str, Any] | None:
        return next((s for s in self.servers if s.get("id") == server_id), None)
