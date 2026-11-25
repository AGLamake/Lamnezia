import secrets
from datetime import datetime
from typing import Any

from django.conf import settings
from django.contrib.auth.models import AbstractUser, UserManager
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _


class Role(models.TextChoices):
    ADMIN = "admin", _("Administrator")
    OWNER = "owner", _("Server administrator")
    MODERATOR = "moderator", _("Moderator")
    USER = "user", _("User")


class Status(models.TextChoices):
    ACTIVE = "active", _("Active")
    PENDING = "pending", _("Waiting for approval")
    BLOCKED = "blocked", _("Blocked")


class Messenger(models.TextChoices):
    TELEGRAM = "telegram", "Telegram"
    DISCORD = "discord", "Discord"


class NotifyVia(models.TextChoices):
    """Where a user gets notifications about their own account."""

    EMAIL = "email", _("Email")
    TELEGRAM = Messenger.TELEGRAM.value, Messenger.TELEGRAM.label
    DISCORD = Messenger.DISCORD.value, Messenger.DISCORD.label


# Roles that work in the admin interface of the panel.
STAFF_ROLES = frozenset({Role.ADMIN, Role.OWNER, Role.MODERATOR})


class AccountManager(UserManager["User"]):
    def create_superuser(
        self, username: str, email: str | None = None, password: str | None = None, **extra: Any
    ) -> "User":
        extra.setdefault("role", Role.ADMIN)
        return super().create_superuser(username, email, password, **extra)


class User(AbstractUser):
    """Panel account. The role gives the access rights; a moderator's are narrowed to their zone.

    A user may be served by several staff members (`managers`, through `Management`).
    """

    role = models.CharField(_("role"), max_length=16, choices=Role.choices, default=Role.USER)
    status = models.CharField(
        _("status"), max_length=16, choices=Status.choices, default=Status.ACTIVE
    )
    invite = models.ForeignKey(
        "accounts.Invite",
        verbose_name=_("invite"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="users",
    )
    notify_via = models.CharField(
        _("notifications"), max_length=16, choices=NotifyVia.choices, default=NotifyVia.EMAIL
    )
    language = models.CharField(
        _("notification language"),
        max_length=16,
        choices=settings.LANGUAGES,
        blank=True,
        help_text=_("Empty: the language of the site."),
    )
    # Staff members answer for their own users and servers (their zone) within their quotas.
    managers = models.ManyToManyField(
        "self",
        verbose_name=_("staff members"),
        through="accounts.Management",
        through_fields=("user", "member"),
        symmetrical=False,
        blank=True,
        related_name="managed_users",
    )
    moderated_servers = models.ManyToManyField(
        "servers.Server",
        verbose_name=_("servers of the staff member"),
        blank=True,
        related_name="moderators",
    )
    user_quota = models.PositiveIntegerField(
        _("users at most"),
        null=True,
        blank=True,
        help_text=_("For a moderator: how many users they may serve. Empty: no limit."),
    )
    key_quota = models.PositiveIntegerField(
        _("keys at most"),
        null=True,
        blank=True,
        help_text=_("For a moderator: how many keys their users may hold. Empty: no limit."),
    )
    # Who made this staff member in the panel: a server administrator's moderators stay theirs.
    created_by = models.ForeignKey(
        "self",
        verbose_name=_("created by"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        editable=False,
        related_name="+",
    )
    # A staff member's level in each section of the panel; a section left out gets the default
    # of the role (`apps/accounts/services/access.py`).
    access = models.JSONField(_("access to sections"), default=dict, blank=True)
    # A portal user's choice; the staff always hear of a sign-in from a new place.
    sign_in_alerts = models.BooleanField(
        _("sign-in alerts"),
        default=False,
        help_text=_("Tell me when my account is signed in from a new network or browser."),
    )
    # The first administrator gets a generated password that the installer leaves in a file:
    # until it is changed, the panel opens only the page to change it.
    must_change_password = models.BooleanField(
        _("must change the password"),
        default=False,
        help_text=_("Opens only the page to change the password until it is changed."),
    )
    # The offer to link a messenger on «My access»: how many times it was put off and until when.
    link_offer_dismissals = models.PositiveSmallIntegerField(default=0, editable=False)
    link_offer_hidden_until = models.DateTimeField(null=True, blank=True, editable=False)

    objects = AccountManager()  # type: ignore[misc]

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")

    @property
    def quota_owner(self) -> "User | None":
        """The server administrator whose quotas bound this moderator's: the one who made them."""
        owner = self.created_by
        if self.role == Role.MODERATOR and owner is not None and owner.role == Role.OWNER:
            return owner
        return None

    def quota_ceiling(self, name: str) -> int | None:
        """The owner's quota `name` this moderator's may not exceed; None: no ceiling."""
        owner = self.quota_owner
        return getattr(owner, name) if owner is not None else None

    def quota_error(self, name: str, value: int | None) -> str | None:
        """Why `value` cannot be this moderator's quota `name`, empty meaning no limit."""
        ceiling = self.quota_ceiling(name)
        if ceiling is None or (value is not None and value <= ceiling):
            return None
        return gettext("At most %(ceiling)d: the quota of the server administrator %(owner)s.") % {
            "ceiling": ceiling,
            "owner": self.quota_owner,
        }

    def effective_quota(self, name: str) -> int | None:
        """The quota `name` in force: the member's own, cut to the owner's if that is lower."""
        own: int | None = getattr(self, name)
        ceiling = self.quota_ceiling(name)
        if ceiling is None:
            return own
        return ceiling if own is None else min(own, ceiling)

    def is_over_user_quota(self, newcomer: "User | None" = None) -> bool:
        """Whether serving one more user, `newcomer` or a new account, would take the moderator
        past their quota."""
        quota = self.effective_quota("user_quota")
        if quota is None:
            return False
        others = self.managed_users.all()
        if newcomer is not None and newcomer.pk is not None:
            others = others.exclude(pk=newcomer.pk)
        return others.count() >= quota

    def save(self, *args: Any, **kwargs: Any) -> None:
        # The Django admin site is for administrators only, so its flags follow the role.
        self.is_staff = self.is_superuser = self.role == Role.ADMIN
        super().save(*args, **kwargs)

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    @property
    def is_staff_member(self) -> bool:
        """Administrator, server administrator or moderator."""
        return self.role in STAFF_ROLES

    @property
    def notification_language(self) -> str:
        """The language notifications to the user are written in: their own or the site's."""
        return self.language or settings.LANGUAGE_CODE

    @property
    def wants_sign_in_alerts(self) -> bool:
        """The staff always hear of a sign-in from a new place; a portal user when they chose to."""
        return self.is_staff_member or self.sign_in_alerts

    @property
    def can_have_keys(self) -> bool:
        """A user waiting for approval or blocked gets no keys (DESIGN.md 6.6)."""
        return self.status == Status.ACTIVE


class Management(models.Model):
    """A staff member serving a user; `via_plan` marks a link that came with the user's plan."""

    user = models.ForeignKey(
        User, verbose_name=_("user"), on_delete=models.CASCADE, related_name="managements"
    )
    member = models.ForeignKey(
        User,
        verbose_name=_("staff member"),
        on_delete=models.CASCADE,
        related_name="+",
        limit_choices_to={"role__in": [Role.OWNER, Role.MODERATOR]},
    )
    via_plan = models.BooleanField(_("from the plan"), default=False)

    class Meta:
        verbose_name = _("staff member of the user")
        verbose_name_plural = _("staff members of the user")
        constraints = (
            models.UniqueConstraint(fields=["user", "member"], name="accounts_management_unique"),
            models.CheckConstraint(
                condition=~models.Q(user=models.F("member")), name="accounts_management_not_self"
            ),
        )

    def __str__(self) -> str:
        return f"{self.member_id} serves {self.user_id}"

    def clean(self) -> None:
        super().clean()
        if self.user_id is None or self.member_id is None:
            return
        if self.user_id == self.member_id:
            raise ValidationError({"member": _("A user cannot be their own moderator.")})
        if self.member.is_over_user_quota(self.user):
            raise ValidationError({"member": _("The moderator has as many users as allowed.")})


def _invite_code() -> str:
    return secrets.token_urlsafe(16)


class Invite(models.Model):
    """A registration link with a plan; works in every registration mode and skips the queue."""

    code = models.CharField(max_length=32, unique=True, default=_invite_code, editable=False)
    plan = models.ForeignKey(
        "plans.Plan", verbose_name=_("plan"), on_delete=models.PROTECT, related_name="invites"
    )
    max_uses = models.PositiveSmallIntegerField(
        _("registrations at most"), default=1, validators=[MinValueValidator(1)]
    )
    uses = models.PositiveSmallIntegerField(_("registrations"), default=0, editable=False)
    expires_at = models.DateTimeField(_("valid until"), null=True, blank=True)
    note = models.CharField(_("note"), max_length=200, blank=True)
    created_by = models.ForeignKey(
        User,
        verbose_name=_("created by"),
        on_delete=models.SET_NULL,
        null=True,
        editable=False,
        related_name="+",
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("invite")
        verbose_name_plural = _("invites")
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return self.note or self.code

    def is_usable(self, now: datetime) -> bool:
        return self.uses < self.max_uses and (self.expires_at is None or now < self.expires_at)


class LinkedAccount(models.Model):
    """A messenger account of a user. One messenger account belongs to one panel user."""

    user = models.ForeignKey(
        User, verbose_name=_("user"), on_delete=models.CASCADE, related_name="linked_accounts"
    )
    messenger = models.CharField(_("messenger"), max_length=16, choices=Messenger.choices)
    # Telegram user id (also the chat id of the private chat with the bot) or Discord user id.
    external_id = models.CharField(_("account id"), max_length=32)
    username = models.CharField(_("name in the messenger"), max_length=100, blank=True)
    linked_at = models.DateTimeField(_("linked at"), auto_now_add=True)

    class Meta:
        verbose_name = _("linked account")
        verbose_name_plural = _("linked accounts")
        ordering = ("messenger",)
        constraints = (
            models.UniqueConstraint(
                fields=["messenger", "external_id"], name="accounts_linked_external_unique"
            ),
            models.UniqueConstraint(
                fields=["user", "messenger"], name="accounts_linked_one_per_messenger"
            ),
        )

    def __str__(self) -> str:
        return f"{self.get_messenger_display()}: {self.username or self.external_id}"


class LinkToken(models.Model):
    """A single-use code that links a messenger account; only its SHA-256 is stored."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    messenger = models.CharField(max_length=16, choices=Messenger.choices)
    digest = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField(db_index=True)

    def __str__(self) -> str:
        return f"{self.get_messenger_display()} code of {self.user_id}"


class UserSession(models.Model):
    """A signed-in browser of a user, for the session list of the profile.

    Only the browser and system names are kept, never the address or the pages visited. The
    session itself lives in Django's session store; a row whose session ended is stale.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sessions")
    session_key = models.CharField(max_length=40, unique=True)
    device = models.CharField(_("device"), max_length=100, blank=True)
    signed_in_at = models.DateTimeField(_("signed in at"), auto_now_add=True)

    class Meta:
        ordering = ("-signed_in_at",)

    def __str__(self) -> str:
        return f"{self.device} of {self.user_id}"


class KnownSignIn(models.Model):
    """A network and a browser the user has signed in from, to tell a sign-in from a new place.

    The address is not kept: `network` is a keyed hash of its network (`SignInPlaces`), which
    only says whether a later sign-in comes from the same one.
    """

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="known_sign_ins")
    network = models.CharField(max_length=64, blank=True)
    device = models.CharField(max_length=100, blank=True)
    last_seen = models.DateTimeField()

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=["user", "network", "device"], name="accounts_known_sign_in_unique"
            ),
        )

    def __str__(self) -> str:
        return f"{self.device} of {self.user_id}"
