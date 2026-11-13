from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.core import mail
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Role, Status, User
from apps.accounts.services.letters import Letters
from apps.accounts.services.registration import (
    Applicant,
    InviteNotValidError,
    Registration,
    RegistrationClosedError,
    TooManyRegistrationsError,
)
from apps.accounts.services.tokens import email_confirmation
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.letters import html_version
from apps.accounts.tests.registration import InviteFactory, configure
from apps.core.models import RegistrationMode
from apps.plans.models import UserLimits
from apps.plans.tests.factories import PlanFactory

pytestmark = pytest.mark.django_db

WITH_MAIL = {"smtp_host": "smtp.example.com"}


@pytest.fixture(autouse=True)
def _clean_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def letters_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Letters wait for the commit, which a test never makes: send them at once."""
    monkeypatch.setattr(transaction, "on_commit", lambda send: send())


def registration(mode: str, ip: str | None = "203.0.113.5", **fields: Any) -> Registration:
    site = configure(registration_mode=mode, **fields)
    return Registration(site, Letters("http://panel.example.com/"), ip=ip)


def applicant(name: str = "alice") -> Applicant:
    return Applicant(name, f"{name}@example.com", "a long password 42")


def plan_of(user: User) -> object:
    limits = UserLimits.objects.filter(user=user).first()
    return limits.plan if limits else None


class TestInviteOnly:
    def test_without_an_invite_registration_is_closed(self) -> None:
        with pytest.raises(RegistrationClosedError):
            registration(RegistrationMode.INVITE_ONLY).register(applicant())

        assert not User.objects.filter(username="alice").exists()

    def test_an_invite_gives_its_plan_and_access_at_once(self) -> None:
        invite = InviteFactory(max_uses=2)

        user = registration(RegistrationMode.INVITE_ONLY).register(applicant(), invite.code)

        invite.refresh_from_db()
        assert (user.status, user.is_active, user.invite) == (Status.ACTIVE, True, invite)
        assert plan_of(user) == invite.plan
        assert invite.uses == 1

    def test_an_expired_invite_does_not_work(self) -> None:
        invite = InviteFactory(expires_at=timezone.now() - timedelta(minutes=1))

        with pytest.raises(InviteNotValidError):
            registration(RegistrationMode.INVITE_ONLY).register(applicant(), invite.code)

    def test_a_used_up_invite_does_not_work(self) -> None:
        invite = InviteFactory(max_uses=1)
        signup = registration(RegistrationMode.INVITE_ONLY)
        signup.register(applicant("alice"), invite.code)

        with pytest.raises(InviteNotValidError):
            signup.register(applicant("bob"), invite.code)
        invite.refresh_from_db()
        assert invite.uses == 1

    def test_an_unknown_code_does_not_work(self) -> None:
        with pytest.raises(InviteNotValidError):
            registration(RegistrationMode.INVITE_ONLY).register(applicant(), "no-such-code")


class TestOpenAuto:
    def test_new_user_gets_the_default_plan(self) -> None:
        default = PlanFactory()

        user = registration(RegistrationMode.OPEN_AUTO, default_plan=default).register(applicant())

        assert (user.status, user.is_active) == (Status.ACTIVE, True)
        assert plan_of(user) == default

    def test_without_a_default_plan_the_user_has_none(self) -> None:
        user = registration(RegistrationMode.OPEN_AUTO).register(applicant())

        assert user.status == Status.ACTIVE
        assert plan_of(user) is None

    def test_the_invite_plan_wins_over_the_default_one(self) -> None:
        invite = InviteFactory()
        signup = registration(RegistrationMode.OPEN_AUTO, default_plan=PlanFactory())

        user = signup.register(applicant(), invite.code)

        assert plan_of(user) == invite.plan


class TestOpenManual:
    def test_new_user_waits_without_a_plan_and_admins_are_told(self, letters_sent: None) -> None:
        UserFactory(role=Role.ADMIN, email="admin@example.com")
        UserFactory(role=Role.USER, email="other@example.com")

        user = registration(RegistrationMode.OPEN_MANUAL).register(applicant())

        assert (user.status, user.is_active, user.can_have_keys) == (Status.PENDING, True, False)
        assert plan_of(user) is None
        [letter] = mail.outbox
        assert letter.to == ["admin@example.com"]
        assert "alice" in letter.body
        assert "http://panel.example.com/accounts/registrations/" in letter.body

    def test_an_invite_skips_the_queue(self, letters_sent: None) -> None:
        UserFactory(role=Role.ADMIN)
        invite = InviteFactory()

        user = registration(RegistrationMode.OPEN_MANUAL).register(applicant(), invite.code)

        assert user.status == Status.ACTIVE
        assert plan_of(user) == invite.plan
        assert mail.outbox == []


class TestEmailConfirmation:
    def test_open_registration_waits_for_the_email(self, letters_sent: None) -> None:
        signup = registration(RegistrationMode.OPEN_AUTO, **WITH_MAIL)

        user = signup.register(applicant())

        assert not user.is_active
        [letter] = mail.outbox
        assert letter.to == ["alice@example.com"]
        assert "http://panel.example.com/accounts/confirm/" in letter.body

    def test_the_letter_has_an_html_version_with_the_same_link(self, letters_sent: None) -> None:
        registration(RegistrationMode.OPEN_AUTO, site_name="My VPN", **WITH_MAIL).register(
            applicant()
        )

        html = html_version(mail.outbox[0])
        assert "My VPN" in html
        assert 'href="http://panel.example.com/accounts/confirm/' in html
        assert "you can link" not in mail.outbox[0].body

    def test_the_letter_tells_about_the_messengers_the_panel_links(
        self, letters_sent: None
    ) -> None:
        bot = {"telegram_bot_token": "123:abc", "telegram_bot_username": "panel_bot"}
        discord = {"discord_client_id": "42", "discord_client_secret": "secret"}
        registration(RegistrationMode.OPEN_AUTO, **bot, **discord, **WITH_MAIL).register(
            applicant()
        )

        [letter] = mail.outbox
        assert "you can link Telegram or Discord" in letter.body
        assert "http://panel.example.com/accounts/messengers/" in letter.body
        assert 'href="http://panel.example.com/accounts/messengers/"' in html_version(letter)

    def test_confirming_activates_and_puts_a_manual_user_in_the_queue(
        self, letters_sent: None
    ) -> None:
        UserFactory(role=Role.ADMIN, email="admin@example.com")
        signup = registration(RegistrationMode.OPEN_MANUAL, **WITH_MAIL)
        user = signup.register(applicant())
        token = email_confirmation.make_token(user)

        signup.confirm_email(user)

        assert user.is_active
        assert user.status == Status.PENDING
        assert [letter.to for letter in mail.outbox] == [
            ["alice@example.com"],
            ["admin@example.com"],
        ]
        assert not email_confirmation.check_token(user, token)

    def test_no_confirmation_without_mail(self) -> None:
        assert registration(RegistrationMode.OPEN_AUTO).register(applicant()).is_active

    def test_no_confirmation_when_turned_off(self) -> None:
        signup = registration(
            RegistrationMode.OPEN_AUTO, require_email_confirmation=False, **WITH_MAIL
        )

        assert signup.register(applicant()).is_active

    def test_invited_users_confirm_the_email_too(self, letters_sent: None) -> None:
        invite = InviteFactory()
        signup = registration(RegistrationMode.INVITE_ONLY, **WITH_MAIL)

        user = signup.register(applicant(), invite.code)

        assert (user.is_active, user.status, plan_of(user)) == (False, Status.ACTIVE, invite.plan)
        assert [letter.to for letter in mail.outbox] == [["alice@example.com"]]
        signup.confirm_email(user)
        assert user.is_active


class TestRateLimit:
    @pytest.fixture(autouse=True)
    def _limit(self, settings: Any) -> None:
        settings.REGISTRATION_RATE_LIMIT = 2

    def test_too_many_registrations_from_one_address(self) -> None:
        signup = registration(RegistrationMode.OPEN_AUTO)
        signup.register(applicant("a1"))
        signup.register(applicant("a2"))

        with pytest.raises(TooManyRegistrationsError):
            signup.register(applicant("a3"))
        assert registration(RegistrationMode.OPEN_AUTO, ip="198.51.100.7").register(applicant("b1"))

    def test_invites_are_not_limited(self) -> None:
        signup = registration(RegistrationMode.OPEN_AUTO)
        invite = InviteFactory(max_uses=5)

        for name in ("a1", "a2", "a3"):
            signup.register(applicant(name), invite.code)

    def test_zero_turns_the_limit_off(self, settings: Any) -> None:
        settings.REGISTRATION_RATE_LIMIT = 0
        signup = registration(RegistrationMode.OPEN_AUTO)

        for name in ("a1", "a2", "a3"):
            signup.register(applicant(name))
