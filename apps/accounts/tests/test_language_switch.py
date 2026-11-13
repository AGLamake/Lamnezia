"""The language switch in the page header sets the language of the user's notifications too."""

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

from apps.accounts.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def switch(client: Client, language: str) -> None:
    response = client.post(reverse("set_language"), {"language": language, "next": "/portal/"})
    assert response.status_code == 302


def test_switching_the_language_sets_the_notification_language(client: Client) -> None:
    user = UserFactory(language="")
    client.force_login(user)

    switch(client, "ru")

    user.refresh_from_db()
    assert user.language == "ru"
    assert client.cookies[settings.LANGUAGE_COOKIE_NAME].value == "ru"


def test_an_unknown_language_leaves_the_profile_as_it_was(client: Client) -> None:
    user = UserFactory(language="en")
    client.force_login(user)

    switch(client, "de")

    user.refresh_from_db()
    assert user.language == "en"


def test_a_guest_switches_the_language_without_a_profile(client: Client) -> None:
    switch(client, "en")

    assert client.cookies[settings.LANGUAGE_COOKIE_NAME].value == "en"
