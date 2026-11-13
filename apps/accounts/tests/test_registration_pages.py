import re
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.core import mail
from django.core.cache import cache
from django.db import transaction
from django.test import Client
from django.utils import timezone

from apps.accounts.models import Invite, Role, Status, User
from apps.accounts.services.tokens import magic_link
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.registration import InviteFactory, configure
from apps.audit.models import AuditEvent
from apps.core.models import RegistrationMode
from apps.keys.forms import KeyIssueForm
from apps.keys.services.actions import KeyActions, OwnerCannotHaveKeysError
from apps.keys.services.zone import KeyZone
from apps.plans.models import UserLimits
from apps.plans.tests.factories import PlanFactory

pytestmark = pytest.mark.django_db

WITH_MAIL = {"smtp_host": "smtp.example.com"}
REGISTER = "/accounts/register/"


@pytest.fixture(autouse=True)
def _clean_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def letters_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Letters wait for the commit, which a test never makes: send them at once."""
    monkeypatch.setattr(transaction, "on_commit", lambda send: send())


@pytest.fixture
def admin_client(client: Client) -> Client:
    client.force_login(UserFactory(role=Role.ADMIN))
    return client


def form(name: str = "alice") -> dict[str, str]:
    password = "a long password 42"
    return {
        "username": name,
        "email": f"{name}@example.com",
        "password1": password,
        "password2": password,
    }


def link_in(letter: mail.EmailMessage) -> str:
    match = re.search(r"http://testserver(\S+)", str(letter.body))
    assert match
    return match.group(1)


class TestRegisterPage:
    def test_closed_without_an_invite_in_invite_only_mode(self, client: Client) -> None:
        response = client.get(REGISTER)

        assert response.status_code == 403
        assert client.get("/accounts/login/").context["registration_open"] is False

    def test_an_invite_opens_it_and_signs_in(self, client: Client) -> None:
        invite = InviteFactory()
        url = f"/accounts/invite/{invite.code}/"

        assert client.get(url).status_code == 200
        response = client.post(url, form())

        assert response["Location"] == "/"
        user = User.objects.get(username="alice")
        assert client.session["_auth_user_id"] == str(user.pk)
        assert UserLimits.objects.get(user=user).plan == invite.plan

    def test_an_expired_invite_is_refused(self, client: Client) -> None:
        invite = InviteFactory(expires_at=timezone.now() - timedelta(days=1))

        assert client.get(f"/accounts/invite/{invite.code}/").status_code == 403

    def test_pending_user_sees_the_waiting_message(self, client: Client) -> None:
        configure(registration_mode=RegistrationMode.OPEN_MANUAL)

        client.post(REGISTER, form())
        # In English: the default language is Russian once the catalogs are compiled.
        response = client.get("/portal/", HTTP_ACCEPT_LANGUAGE="en")

        assert response.status_code == 200
        assert response.context["user"].status == Status.PENDING
        assert "waits for approval" in response.content.decode()

    def test_one_email_is_one_account(self, client: Client) -> None:
        configure(registration_mode=RegistrationMode.OPEN_AUTO)
        UserFactory(email="Alice@example.com")

        response = client.post(REGISTER, form())

        assert response.status_code == 200
        assert "email" in response.context["form"].errors

    def test_rate_limit_shows_an_error(self, client: Client, settings: Any) -> None:
        settings.REGISTRATION_RATE_LIMIT = 1
        configure(registration_mode=RegistrationMode.OPEN_AUTO)
        client.post(REGISTER, form("a1"))
        client.logout()

        response = client.post(REGISTER, form("a2"))

        assert response.status_code == 200
        assert response.context["form"].non_field_errors()
        assert not User.objects.filter(username="a2").exists()

    def test_email_confirmation_link_signs_in(self, client: Client, letters_sent: None) -> None:
        configure(registration_mode=RegistrationMode.OPEN_AUTO, **WITH_MAIL)

        response = client.post(REGISTER, form())
        assert "Check your email" in response.content.decode()
        link = link_in(mail.outbox[0])

        assert client.get(link)["Location"] == "/"
        assert User.objects.get(username="alice").is_active
        client.logout()
        assert client.get(link).status_code == 403

    def test_a_blocked_account_is_not_confirmed(self, client: Client, letters_sent: None) -> None:
        configure(registration_mode=RegistrationMode.OPEN_AUTO, **WITH_MAIL)
        client.post(REGISTER, form())
        User.objects.filter(username="alice").update(status=Status.BLOCKED)

        assert client.get(link_in(mail.outbox[0])).status_code == 403
        assert not User.objects.get(username="alice").is_active


class TestMagicLink:
    def test_needs_mail(self, client: Client) -> None:
        assert client.get("/accounts/link/").status_code == 404
        assert client.get("/accounts/login/").context["magic_link"] is False

    def test_link_signs_in_once(self, client: Client, letters_sent: None) -> None:
        configure(**WITH_MAIL)
        user = UserFactory(email="alice@example.com")

        client.post("/accounts/link/", {"email": "ALICE@example.com"})
        link = link_in(mail.outbox[0])

        assert client.get(link).status_code == 200
        assert "_auth_user_id" not in client.session
        assert client.post(link)["Location"] == "/"
        assert client.session["_auth_user_id"] == str(user.pk)
        client.logout()
        assert client.post(link).status_code == 403

    def test_unknown_email_gets_the_same_answer(self, client: Client, letters_sent: None) -> None:
        configure(**WITH_MAIL)

        response = client.post("/accounts/link/", {"email": "nobody@example.com"})

        assert response["Location"] == "/accounts/login/"
        assert mail.outbox == []

    def test_expired_link_does_not_work(self, settings: Any) -> None:
        user = UserFactory()
        token = magic_link.make(user)
        settings.MAGIC_LINK_MINUTES = -1

        assert magic_link.user(token) is None

    def test_tampered_link_does_not_work(self) -> None:
        assert magic_link.user(magic_link.make(UserFactory()) + "x") is None


class TestAdminPages:
    def test_for_the_staff_only(self, client: Client) -> None:
        client.force_login(UserFactory(role=Role.USER))

        assert client.get("/accounts/registrations/").status_code == 403

    def test_approve_gives_the_plan(self, admin_client: Client) -> None:
        applicant = UserFactory(status=Status.PENDING)
        plan = PlanFactory()
        assert list(admin_client.get("/accounts/registrations/").context["waiting"]) == [applicant]

        admin_client.post(f"/accounts/registrations/{applicant.pk}/approve/", {"plan": plan.pk})

        applicant.refresh_from_db()
        assert applicant.status == Status.ACTIVE
        assert UserLimits.objects.get(user=applicant).plan == plan
        assert AuditEvent.objects.filter(action="accounts.approve").exists()

    def test_reject_blocks(self, admin_client: Client) -> None:
        applicant = UserFactory(status=Status.PENDING)

        admin_client.post(f"/accounts/registrations/{applicant.pk}/reject/")

        applicant.refresh_from_db()
        assert (applicant.status, applicant.is_active) == (Status.BLOCKED, False)

    def test_create_and_delete_an_invite(self, admin_client: Client) -> None:
        plan = PlanFactory()

        admin_client.post("/accounts/invites/add/", {"plan": plan.pk, "max_uses": 3})
        invite = Invite.objects.get()
        page = admin_client.get("/accounts/registrations/").content.decode()
        admin_client.post(f"/accounts/invites/{invite.pk}/delete/")

        assert (invite.plan, invite.max_uses) == (plan, 3)
        assert f"http://testserver/accounts/invite/{invite.code}/" in page
        assert not Invite.objects.exists()


class TestPendingUserGetsNoKeys:
    def test_key_actions_refuse(self) -> None:
        pending = UserFactory(status=Status.PENDING)

        with pytest.raises(OwnerCannotHaveKeysError):
            KeyActions(UserFactory(role=Role.ADMIN)).issue("any", "iPhone", pending)

    def test_issue_form_does_not_offer_them(self) -> None:
        pending = UserFactory(status=Status.PENDING)
        active = UserFactory()

        form = KeyIssueForm(zone=KeyZone(UserFactory(role=Role.ADMIN)))
        users = form.fields["user"].queryset  # type: ignore[attr-defined]

        assert active in users
        assert pending not in users
