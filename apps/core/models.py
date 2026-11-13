from typing import Any

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.ssh.encryption import EncryptedTextField


class RegistrationMode(models.TextChoices):
    INVITE_ONLY = "invite_only", _("By invitation only")
    OPEN_AUTO = "open_auto", _("Open, the default plan right away")
    OPEN_MANUAL = "open_manual", _("Open, an administrator approves")


class KeyRevealPolicy(models.TextChoices):
    ALWAYS = "always", _("Any time")
    ONCE = "once", _("Only once")


class BotKeyDelivery(models.TextChoices):
    """Whether the bots may send the key itself: Telegram and Discord chats are not end-to-end
    encrypted.
    """

    ALLOWED = "allowed", _("The key itself")
    LINK_ONLY = "link_only", _("Only a link to the portal")


class SmtpSecurity(models.TextChoices):
    STARTTLS = "starttls", "STARTTLS"
    SSL = "ssl", "SSL/TLS"
    NONE = "none", _("None")


class SiteSettings(models.Model):
    """Panel settings an administrator edits in the interface. There is exactly one row."""

    site_name = models.CharField(_("panel name"), max_length=64, default="Lamnezia")
    registration_mode = models.CharField(
        _("registration"),
        max_length=16,
        choices=RegistrationMode.choices,
        default=RegistrationMode.INVITE_ONLY,
    )
    default_plan = models.ForeignKey(
        "plans.Plan",
        verbose_name=_("default plan"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text=_("Given to new users in open registration with automatic approval."),
    )
    require_email_confirmation = models.BooleanField(
        _("confirm email on registration"), default=True
    )
    key_reveal_policy = models.CharField(
        _("showing a key again"),
        max_length=16,
        choices=KeyRevealPolicy.choices,
        default=KeyRevealPolicy.ALWAYS,
    )
    bot_key_delivery = models.CharField(
        _("keys in the bots"),
        max_length=16,
        choices=BotKeyDelivery.choices,
        default=BotKeyDelivery.ALLOWED,
        help_text=_(
            "Telegram and Discord keep chats on their servers without end-to-end encryption."
        ),
    )
    guide_suggestions = models.BooleanField(
        _("users suggest articles"),
        default=True,
        help_text=_("Signed-in users send links and texts for the guides; the staff review them."),
    )
    smtp_host = models.CharField(_("SMTP server"), max_length=255, blank=True)
    smtp_port = models.PositiveIntegerField(_("SMTP port"), default=587)
    smtp_security = models.CharField(
        _("encryption"), max_length=16, choices=SmtpSecurity.choices, default=SmtpSecurity.STARTTLS
    )
    smtp_username = models.CharField(_("SMTP user"), max_length=255, blank=True)
    smtp_password = EncryptedTextField(_("SMTP password"), blank=True)
    from_email = models.CharField(_("sender"), max_length=255, blank=True)
    telegram_bot_token = EncryptedTextField(_("Telegram bot token"), blank=True)
    telegram_bot_username = models.CharField(
        _("Telegram bot username"),
        max_length=64,
        blank=True,
        help_text=_("Without @. Users link their Telegram through this bot."),
    )
    telegram_login = models.BooleanField(
        _("sign in with Telegram"),
        default=False,
        help_text=_(
            "Users with a linked Telegram can sign in with it. Set the domain in @BotFather."
        ),
    )
    discord_client_id = models.CharField(
        _("Discord application ID"),
        max_length=32,
        blank=True,
        help_text=_("From the Discord Developer Portal; users link their Discord through it."),
    )
    discord_client_secret = EncryptedTextField(_("Discord client secret"), blank=True)
    discord_bot_token = EncryptedTextField(_("Discord bot token"), blank=True)
    discord_login = models.BooleanField(
        _("sign in with Discord"),
        default=False,
        help_text=_("Users with a linked Discord can sign in with it."),
    )
    github_token = EncryptedTextField(_("GitHub token"), blank=True)
    webhooks_private_networks = models.BooleanField(
        _("notifications to private networks"),
        default=False,
        help_text=_(
            "Webhooks may go to addresses such as 127.0.0.1, 10.0.0.0/8 or 192.168.0.0/16. "
            "Turn on only for a receiver in your own network."
        ),
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("panel settings")
        verbose_name_plural = _("panel settings")

    def __str__(self) -> str:
        return str(self._meta.verbose_name)

    def save(self, *args: Any, **kwargs: Any) -> None:
        self.pk = 1
        super().save(*args, **kwargs)

    @property
    def registration_open(self) -> bool:
        """Anyone may register, not only those with an invite."""
        return self.registration_mode != RegistrationMode.INVITE_ONLY

    @property
    def telegram_linking(self) -> bool:
        """Users can link a Telegram account: the panel bot is set up."""
        return bool(self.telegram_bot_token and self.telegram_bot_username)

    @property
    def discord_oauth(self) -> bool:
        """Users can link a Discord account in the browser: the Discord application is set up."""
        return bool(self.discord_client_id and self.discord_client_secret)

    @classmethod
    def load(cls) -> "SiteSettings":
        settings, _created = cls.objects.get_or_create(pk=1)
        return settings


class Cleanup(models.Model):
    """When a housekeeping task of the beat last ran and how many rows it removed."""

    task = models.CharField(_("task"), max_length=64, unique=True)
    ran_at = models.DateTimeField(_("ran at"))
    removed = models.PositiveIntegerField(_("rows removed"), default=0)

    class Meta:
        verbose_name = _("cleanup")
        verbose_name_plural = _("cleanups")

    def __str__(self) -> str:
        return self.task

    @classmethod
    def record(cls, task: str, removed: int) -> int:
        """Note that `task` ran now; return `removed`, so the task returns it on."""
        cls.objects.update_or_create(
            task=task, defaults={"ran_at": timezone.now(), "removed": removed}
        )
        return removed
