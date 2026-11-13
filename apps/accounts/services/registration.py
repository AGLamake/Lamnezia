"""New accounts by the registration mode of the panel and by invites (DESIGN.md 6.6)."""

from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Invite, Role, Status, User
from apps.accounts.services.letters import Letters
from apps.core.mail import mail_configured
from apps.core.models import RegistrationMode, SiteSettings
from apps.notifications.events import Event
from apps.notifications.services.delivery import emit
from apps.plans.models import Plan
from apps.plans.services.limits import assign_plan

_HOUR = 60 * 60


class RegistrationError(Exception):
    message = _("Registration failed.")

    def __str__(self) -> str:
        return str(self.message)


class RegistrationClosedError(RegistrationError):
    message = _("Registration is by invitation only.")


class InviteNotValidError(RegistrationError):
    message = _("This invite has expired or has been used up.")


class ModeratorFullError(RegistrationError):
    message = _("This invite cannot take new users now.")


class TooManyRegistrationsError(RegistrationError):
    message = _("Too many registrations from your address. Try again in an hour.")


@dataclass(frozen=True)
class Applicant:
    username: str
    email: str
    password: str


class RegistrationThrottle:
    """Counts open registrations from one address over an hour (REGISTRATION_RATE_LIMIT)."""

    def __init__(self, ip: str | None) -> None:
        self._key = f"accounts:registrations:{ip}" if ip else None

    def check(self) -> None:
        limit = settings.REGISTRATION_RATE_LIMIT
        if self._key and limit and cache.get(self._key, 0) >= limit:
            raise TooManyRegistrationsError

    def record(self) -> None:
        if self._key:
            cache.add(self._key, 0, timeout=_HOUR)
            cache.incr(self._key)


class Registration:
    """An invite gives its plan and skips the queue in every mode; a moderator's invite also
    puts the user into the moderator's zone, while the moderator's user quota allows.

    Without one, `open_auto` gives the default plan and `open_manual` puts the user in the
    queue (status `pending`). Every registration, by invite too, confirms the email when mail is set
    up and the panel asks for it: an invite says who may join, not that the address is theirs.
    """

    def __init__(self, site: SiteSettings, letters: Letters, ip: str | None = None) -> None:
        self._site = site
        self._letters = letters
        self._throttle = RegistrationThrottle(ip)

    @property
    def is_open(self) -> bool:
        return self._site.registration_open

    @property
    def confirms_email(self) -> bool:
        return self._site.require_email_confirmation and mail_configured()

    @staticmethod
    def invite(code: str, now: datetime | None = None, *, lock: bool = False) -> Invite:
        """The invite by its code, if it still works."""
        invites = Invite.objects.select_for_update() if lock else Invite.objects.all()
        invite = invites.filter(code=code).first()
        if invite is None or not invite.is_usable(now or timezone.now()):
            raise InviteNotValidError
        return invite

    def register(self, applicant: Applicant, invite_code: str | None = None) -> User:
        now = timezone.now()
        with transaction.atomic():
            invite = self._take_invite(invite_code, now) if invite_code else None
            if invite is None:
                self._check_open()
            user = self._create(applicant, invite)
            plan = invite.plan if invite else self._open_plan()
            if plan is not None:
                assign_plan(user, plan, now)
            self._announce(user)
        if invite is None:
            self._throttle.record()
        return user

    def confirm_email(self, user: User) -> None:
        user.is_active = True
        user.save(update_fields=["is_active"])
        if user.status == Status.PENDING:
            self._new_application(user)

    def _take_invite(self, code: str, now: datetime) -> Invite:
        # Locked: two registrations at once must not use an invite beyond its limit, nor take
        # the moderator past their user quota.
        invite = self.invite(code, now, lock=True)
        moderator = self._moderator(invite)
        if moderator is not None:
            User.objects.select_for_update().filter(pk=moderator.pk).exists()
            if moderator.is_over_user_quota():
                raise ModeratorFullError
        invite.uses += 1
        invite.save(update_fields=["uses"])
        return invite

    def _check_open(self) -> None:
        if not self.is_open:
            raise RegistrationClosedError
        self._throttle.check()

    def _create(self, applicant: Applicant, invite: Invite | None) -> User:
        manual = invite is None and self._site.registration_mode == RegistrationMode.OPEN_MANUAL
        user = User(
            username=applicant.username,
            email=applicant.email,
            invite=invite,
            manager=self._moderator(invite) if invite else None,
            status=Status.PENDING if manual else Status.ACTIVE,
            is_active=not self.confirms_email,
        )
        user.set_password(applicant.password)
        user.save()
        return user

    @staticmethod
    def _moderator(invite: Invite) -> User | None:
        """The moderator who made the invite and whose zone its users join."""
        author = invite.created_by
        return author if author is not None and author.role == Role.MODERATOR else None

    def _open_plan(self) -> Plan | None:
        if self._site.registration_mode == RegistrationMode.OPEN_AUTO:
            return self._site.default_plan
        return None

    def _announce(self, user: User) -> None:
        if not user.is_active:
            self._letters.confirm_email(user)
        elif user.status == Status.PENDING:
            self._new_application(user)

    def _new_application(self, user: User) -> None:
        self._letters.new_application(user)
        emit(
            Event.REGISTRATION_PENDING,
            {"user": user.username, "email": user.email},
            key=f"registration_pending:{user.pk}",
        )
