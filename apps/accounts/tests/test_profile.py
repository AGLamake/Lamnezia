from collections.abc import Iterator

import pytest
from django.contrib.sessions.models import Session
from django.core import mail
from django.core.cache import cache
from django.db import transaction
from django.test import Client

from apps.accounts.models import User, UserSession
from apps.accounts.services.sessions import device_name
from apps.accounts.tests.factories import UserFactory
from apps.accounts.tests.registration import configure
from apps.audit.models import AuditEvent

pytestmark = pytest.mark.django_db

PROFILE = "/accounts/profile/"
FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"
IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)


@pytest.fixture(autouse=True)
def _clean_cache() -> Iterator[None]:
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def user() -> User:
    return UserFactory(username="alice", email="alice@example.com")


def signed_in(user: User, agent: str = FIREFOX) -> Client:
    client = Client(HTTP_USER_AGENT=agent)
    client.post("/accounts/login/", {"username": user.username, "password": "password"})
    return client


def test_the_profile_has_every_section(user: User) -> None:
    page = signed_in(user).get(PROFILE).content.decode()

    for section in ("account", "password", "two-factor", "messengers", "notifications"):
        assert f'id="{section}"' in page
    assert 'id="sessions"' in page and "alice@example.com" in page
    assert "autofocus" not in page


def test_old_pages_lead_to_their_sections(user: User) -> None:
    client = signed_in(user)

    assert client.get("/accounts/security/")["Location"] == f"{PROFILE}#two-factor"
    assert client.get("/accounts/messengers/")["Location"] == f"{PROFILE}#messengers"
    assert client.get("/accounts/password/")["Location"] == f"{PROFILE}#password"


@pytest.mark.parametrize(
    ("agent", "name"),
    [
        (FIREFOX, "Firefox · Windows"),
        (IPHONE, "Safari · iOS"),
        ("Mozilla/5.0 (X11; Linux x86_64) Chrome/126.0 Safari/537.36 Edg/126.0", "Edge · Linux"),
        ("curl/8.0", ""),
    ],
)
def test_a_session_is_named_by_the_browser_and_system(agent: str, name: str) -> None:
    assert device_name(agent) == name


class TestSessions:
    def test_each_sign_in_is_listed_and_the_current_one_comes_first(self, user: User) -> None:
        signed_in(user, IPHONE)
        laptop = signed_in(user)

        sessions = laptop.get(PROFILE).context["sessions"]

        assert [(s.device, s.current) for s in sessions] == [
            ("Firefox · Windows", True),
            ("Safari · iOS", False),
        ]

    def test_the_page_never_shows_session_keys(self, user: User) -> None:
        signed_in(user, IPHONE)
        laptop = signed_in(user)

        page = laptop.get(PROFILE).content.decode()

        assert not any(key in page for key in Session.objects.values_list("session_key", flat=True))

    def test_another_session_can_be_ended(self, user: User) -> None:
        phone = signed_in(user, IPHONE)
        laptop = signed_in(user)
        other = next(s for s in laptop.get(PROFILE).context["sessions"] if not s.current)

        response = laptop.post("/accounts/profile/sessions/end/", {"session": other.id})

        assert response["Location"] == f"{PROFILE}#sessions"
        assert phone.get(PROFILE)["Location"].startswith("/accounts/login/")
        assert laptop.get(PROFILE).status_code == 200
        assert AuditEvent.objects.get(action="accounts.sessions_end").data == {"count": 1}

    def test_the_current_session_and_strangers_are_not_ended(self, user: User) -> None:
        laptop = signed_in(user)
        stranger = signed_in(UserFactory())
        mine = UserSession.objects.get(user=user)
        theirs = UserSession.objects.exclude(user=user).get()

        laptop.post("/accounts/profile/sessions/end/", {"session": mine.pk})
        laptop.post("/accounts/profile/sessions/end/", {"session": theirs.pk})

        assert laptop.get(PROFILE).status_code == 200
        assert stranger.get(PROFILE).status_code == 200

    def test_sign_out_everywhere_else(self, user: User) -> None:
        phone = signed_in(user, IPHONE)
        tablet = signed_in(user, IPHONE)
        laptop = signed_in(user)

        laptop.post("/accounts/profile/sessions/end/", {"session": "others"})

        assert [s.current for s in laptop.get(PROFILE).context["sessions"]] == [True]
        assert phone.get(PROFILE).status_code == tablet.get(PROFILE).status_code == 302

    def test_logging_out_forgets_the_session(self, user: User) -> None:
        client = signed_in(user)

        client.post("/accounts/logout/")

        assert not UserSession.objects.exists()

    def test_a_new_password_signs_out_the_other_sessions(self, user: User) -> None:
        phone = signed_in(user, IPHONE)
        laptop = signed_in(user)
        password = "a-new-long-passw0rd"

        response = laptop.post(
            "/accounts/password/",
            {"old_password": "password", "new_password1": password, "new_password2": password},
        )

        assert response["Location"] == f"{PROFILE}#password"
        assert phone.get(PROFILE).status_code == 302
        assert [s.current for s in laptop.get(PROFILE).context["sessions"]] == [True]

    def test_a_wrong_password_shows_the_profile_with_the_error(self, user: User) -> None:
        response = signed_in(user).post(
            "/accounts/password/",
            {"old_password": "wrong", "new_password1": "x", "new_password2": "x"},
        )

        assert response.status_code == 200
        assert response.context["password_form"].errors
        assert 'id="sessions"' in response.content.decode()


class TestEmailChange:
    URL = "/accounts/profile/email/"

    def test_without_mail_the_email_changes_at_once(self, user: User) -> None:
        client = signed_in(user)

        response = client.post(self.URL, {"email": "new@example.com", "password": "password"})

        assert response["Location"] == f"{PROFILE}#account"
        user.refresh_from_db()
        assert user.email == "new@example.com"
        assert AuditEvent.objects.filter(action="accounts.email_change").exists()

    def test_the_password_and_a_free_address_are_required(self, user: User) -> None:
        UserFactory(email="taken@example.com")
        client = signed_in(user)

        wrong = client.post(self.URL, {"email": "new@example.com", "password": "nope"})
        taken = client.post(self.URL, {"email": "TAKEN@example.com", "password": "password"})

        assert wrong.context["email_form"].errors.keys() == {"password"}
        assert taken.context["email_form"].errors.keys() == {"email"}
        user.refresh_from_db()
        assert user.email == "alice@example.com"

    def test_with_mail_the_new_address_confirms_itself(
        self, user: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure(smtp_host="smtp.example.com")
        monkeypatch.setattr(transaction, "on_commit", lambda send: send())
        client = signed_in(user)

        client.post(self.URL, {"email": "new@example.com", "password": "password"})

        user.refresh_from_db()
        assert user.email == "alice@example.com"
        (letter,) = mail.outbox
        assert letter.to == ["new@example.com"]
        link = next(word for word in letter.body.split() if "/profile/email/" in word)
        assert client.get(link)["Location"] == f"{PROFILE}#account"
        user.refresh_from_db()
        assert user.email == "new@example.com"
        client.get(link)
        assert AuditEvent.objects.filter(action="accounts.email_change").count() == 1

    def test_a_link_of_another_account_changes_nothing(
        self, user: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure(smtp_host="smtp.example.com")
        monkeypatch.setattr(transaction, "on_commit", lambda send: send())
        signed_in(user).post(self.URL, {"email": "new@example.com", "password": "password"})
        link = next(word for word in mail.outbox[0].body.split() if "/profile/email/" in word)

        signed_in(UserFactory(email="bob@example.com")).get(link)

        assert not User.objects.filter(email="new@example.com").exists()
