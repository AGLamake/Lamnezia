from urllib.parse import parse_qs, urlsplit

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.forms import PasswordChangeForm, SetPasswordForm
from apps.accounts.models import User
from apps.accounts.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

NEW_PASSWORD = reverse("accounts:password-required")
PROFILE = reverse("accounts:profile")
STRONG = "a-new-long-passw0rd"


@pytest.fixture
def held(client: Client) -> User:
    user: User = UserFactory(must_change_password=True)
    client.force_login(user)
    return user


def change(client: Client, next_page: str = PROFILE) -> None:
    client.post(
        NEW_PASSWORD,
        {
            "old_password": "password",
            "new_password1": STRONG,
            "new_password2": STRONG,
            "next": next_page,
        },
    )


def test_every_page_leads_to_the_new_password(client: Client, held: User) -> None:
    response = client.get(PROFILE)

    target = urlsplit(response["Location"])
    assert (target.path, parse_qs(target.query)["next"]) == (NEW_PASSWORD, [PROFILE])


def test_htmx_request_sends_the_whole_page(client: Client, held: User) -> None:
    response = client.get(
        "/support/unread/",
        headers={"HX-Request": "true", "HX-Current-URL": "http://testserver/portal/"},
    )

    assert urlsplit(response["HX-Redirect"]).path == NEW_PASSWORD


def test_page_and_signing_out_stay_open(client: Client, held: User) -> None:
    assert client.get(NEW_PASSWORD).status_code == 200
    client.post(reverse("accounts:logout"))

    assert "_auth_user_id" not in client.session


def test_new_password_lets_the_user_in(client: Client, held: User) -> None:
    change(client)

    held.refresh_from_db()
    assert held.check_password(STRONG)
    assert not held.must_change_password
    assert client.get(PROFILE).status_code == 200


def test_after_the_change_goes_where_the_user_was_going(client: Client, held: User) -> None:
    response = client.post(
        NEW_PASSWORD,
        {
            "old_password": "password",
            "new_password1": STRONG,
            "new_password2": STRONG,
            "next": "https://evil.example/",
        },
    )

    assert response["Location"] == reverse("home")


def test_wrong_old_password_keeps_the_mark(client: Client, held: User) -> None:
    client.post(
        NEW_PASSWORD,
        {"old_password": "wrong", "new_password1": STRONG, "new_password2": STRONG},
    )

    held.refresh_from_db()
    assert held.must_change_password


def test_user_without_the_mark_is_not_held(client: Client) -> None:
    client.force_login(UserFactory())

    assert client.get(PROFILE).status_code == 200


@pytest.mark.parametrize(
    "form_class, data",
    [
        (PasswordChangeForm, {"old_password": "password"}),
        (SetPasswordForm, {}),
    ],
)
def test_any_chosen_password_ends_the_temporary_one(
    form_class: type[SetPasswordForm], data: dict[str, str]
) -> None:
    user: User = UserFactory(must_change_password=True)
    form = form_class(user, data | {"new_password1": STRONG, "new_password2": STRONG})

    assert form.is_valid(), form.errors
    form.save()

    user.refresh_from_db()
    assert not user.must_change_password
