"""A sign-in from a new place is told to the user, a lockout to the staff; never a password."""

import json
from typing import Any

import pytest
from django.test import Client

from apps.accounts.models import Role, User
from apps.accounts.tests.factories import UserFactory
from apps.notifications.events import EVENTS, Event
from apps.notifications.models import Delivery, Notification, Route
from apps.notifications.tests.factories import ChannelFactory

pytestmark = pytest.mark.django_db

LOGIN = "/accounts/login/"
FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"
CHROME = (
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Mobile Safari/537.36"
)


def sign_in(user: User, ip: str, agent: str = FIREFOX, password: str = "password") -> int:
    response = Client().post(
        LOGIN,
        {"username": user.username, "password": password},
        REMOTE_ADDR=ip,
        HTTP_USER_AGENT=agent,
    )
    return response.status_code


def alerts(user: User) -> list[Notification]:
    return list(Notification.objects.filter(event=Event.SIGN_IN_NEW_PLACE, user=user))


class TestNewPlace:
    def test_staff_hear_of_a_new_network(self) -> None:
        admin = UserFactory(role=Role.ADMIN)
        sign_in(admin, "203.0.113.5")

        sign_in(admin, "198.51.100.9")

        [alert] = alerts(admin)
        assert alert.data["address"] == "198.51.100.9"
        assert alert.data["device"] == "Firefox · Windows"
        assert alert.data["url"].endswith("/accounts/profile/#sessions")
        assert Delivery.objects.filter(notification=alert, to_user=True).exists()

    def test_staff_hear_of_a_new_browser(self) -> None:
        moderator = UserFactory(role=Role.MODERATOR)
        sign_in(moderator, "203.0.113.5")

        sign_in(moderator, "203.0.113.5", CHROME)

        [alert] = alerts(moderator)
        assert alert.data["device"] == "Chrome · Android"

    def test_a_known_place_is_not_news(self) -> None:
        admin = UserFactory(role=Role.ADMIN)
        sign_in(admin, "203.0.113.5")

        sign_in(admin, "203.0.113.5")

        assert alerts(admin) == []

    def test_the_first_sign_in_is_not_news(self) -> None:
        admin = UserFactory(role=Role.ADMIN)

        sign_in(admin, "203.0.113.5")

        assert alerts(admin) == []

    def test_a_portal_user_hears_only_when_they_chose_to(self) -> None:
        quiet, careful = UserFactory(), UserFactory(sign_in_alerts=True)
        for user in (quiet, careful):
            sign_in(user, "203.0.113.5")
            sign_in(user, "198.51.100.9")

        assert alerts(quiet) == []
        assert len(alerts(careful)) == 1

    def test_the_message_holds_no_password(self) -> None:
        admin = UserFactory(role=Role.ADMIN)
        sign_in(admin, "203.0.113.5")
        sign_in(admin, "198.51.100.9")

        [alert] = alerts(admin)
        _, text = EVENTS[Event.SIGN_IN_NEW_PLACE].render(alert.data)
        assert "password" not in json.dumps(alert.data)
        assert "198.51.100.9" in text
        assert "#sessions" in text


class TestLockout:
    @pytest.fixture(autouse=True)
    def _routed(self, settings: Any) -> None:
        settings.AXES_FAILURE_LIMIT = 3
        Route.objects.create(channel=ChannelFactory(), event=Event.SIGN_IN_LOCKED)

    def test_the_staff_hear_once_of_a_lockout(self) -> None:
        user = UserFactory()
        for _ in range(5):
            sign_in(user, "203.0.113.5", password="hunter2-guess")

        [alert] = Notification.objects.filter(event=Event.SIGN_IN_LOCKED)
        assert alert.data == {
            "user": user.username,
            "address": "203.0.113.5",
            "failures": 3,
            "minutes": 15,
        }
        assert "hunter2" not in json.dumps(alert.data)

    def test_a_lockout_from_another_address_is_told_too(self) -> None:
        user = UserFactory()
        for ip in ("203.0.113.5", "198.51.100.9"):
            for _ in range(3):
                sign_in(user, ip, password="wrong")

        assert Notification.objects.filter(event=Event.SIGN_IN_LOCKED).count() == 2

    def test_wrong_passwords_below_the_limit_are_not_told(self) -> None:
        user = UserFactory()
        for _ in range(2):
            sign_in(user, "203.0.113.5", password="wrong")

        assert not Notification.objects.filter(event=Event.SIGN_IN_LOCKED).exists()
